"""OwVoice 本地后端：管理人物模型并代理 GPT-SoVITS API。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_DIR / "config"
CACHE_DIR = PROJECT_DIR / ".cache" / "synthesis"
CONFIG_PATH = Path(os.environ.get("OWVOICE_CONFIG", CONFIG_DIR / "voices.local.json"))
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = PROJECT_DIR / CONFIG_PATH
GSV_API = os.environ.get("OWVOICE_GSV_API", "http://127.0.0.1:9880").rstrip("/")

app = FastAPI(title="OwVoice API", version="0.1.0")
_model_lock = threading.Lock()
_active_model_key: str | None = None
_active_voice_id: str | None = None


class SynthesizeRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=10000)
    speed: float = Field(default=1.0, ge=0.1, le=3.0)
    top_k: int | None = Field(default=None, ge=1, le=100)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    temperature: float | None = Field(default=None, gt=0.0, le=2.0)
    sample_steps: int | None = Field(default=None, ge=4, le=128)
    cut_punc: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=64)


def load_voices() -> list[dict[str, Any]]:
    if not CONFIG_PATH.exists():
        example = CONFIG_DIR / "voices.example.json"
        raise RuntimeError(
            f"找不到人物配置：{CONFIG_PATH}。请先复制 {example.name} 为 voices.local.json。"
        )
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"人物配置 JSON 格式错误：{exc}") from exc

    voices = [voice for voice in data.get("voices", []) if voice.get("enabled", True)]
    if not voices:
        raise RuntimeError("人物配置中没有启用的角色")
    return voices


def find_voice(voice_id: str) -> dict[str, Any]:
    for voice in load_voices():
        if voice.get("id") == voice_id:
            return voice
    raise HTTPException(status_code=404, detail=f"未找到角色：{voice_id}")


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else PROJECT_DIR / path


def availability(voice: dict[str, Any]) -> dict[str, bool]:
    gpt = resolve_path(voice.get("gpt_model"))
    sovits = resolve_path(voice.get("sovits_model"))
    reference = resolve_path(voice.get("reference_audio"))
    return {
        "gpt_model": bool(gpt and gpt.is_file()),
        "sovits_model": bool(sovits and sovits.is_file()),
        "reference_audio": bool(reference and reference.is_file()),
    }


def model_key_for_voice(voice: dict[str, Any]) -> str | None:
    """生成角色模型组合的稳定键，用于避免重复加载同一组模型。"""
    gpt_path = resolve_path(voice.get("gpt_model"))
    sovits_path = resolve_path(voice.get("sovits_model"))
    if not gpt_path or not sovits_path:
        return None
    return f"{gpt_path.resolve()}|{sovits_path.resolve()}"


def initialize_active_voice_from_environment() -> None:
    """同步启动器已传入的首个角色，避免再次调用 GPT-SoVITS /set_model。"""
    global _active_model_key, _active_voice_id

    initial_voice_id = os.environ.get("OWVOICE_INITIAL_VOICE_ID", "").strip()
    if not initial_voice_id:
        return
    try:
        voice = next((item for item in load_voices() if item.get("id") == initial_voice_id), None)
        model_key = model_key_for_voice(voice) if voice else None
        if voice and model_key:
            _active_model_key = model_key
            _active_voice_id = initial_voice_id
    except (OSError, RuntimeError, json.JSONDecodeError):
        # 启动器会在真正合成前重新校验模型；这里不能阻止后端启动。
        return


def engine_online() -> bool:
    try:
        response = requests.get(f"{GSV_API}/control", timeout=2)
        return response.status_code in (200, 404, 405)
    except requests.RequestException:
        return False


initialize_active_voice_from_environment()


def activate_voice(voice: dict[str, Any]) -> None:
    global _active_model_key, _active_voice_id

    gpt_path = resolve_path(voice.get("gpt_model"))
    sovits_path = resolve_path(voice.get("sovits_model"))
    if not gpt_path or not sovits_path:
        raise HTTPException(status_code=422, detail="角色没有配置完整的 GPT/SoVITS 模型")
    if not gpt_path.is_file() or not sovits_path.is_file():
        raise HTTPException(
            status_code=422,
            detail=f"角色模型不存在：gpt={gpt_path}, sovits={sovits_path}",
        )

    model_key = model_key_for_voice(voice)
    if model_key is None:
        raise HTTPException(status_code=422, detail="角色模型路径无效")
    with _model_lock:
        if model_key == _active_model_key:
            _active_voice_id = voice["id"]
            return
        try:
            response = requests.post(
                f"{GSV_API}/set_model",
                json={
                    "gpt_model_path": str(gpt_path),
                    "sovits_model_path": str(sovits_path),
                },
                timeout=600,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=503, detail=f"模型切换请求失败：{exc}") from exc
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"GPT-SoVITS 模型切换失败：{response.text}")
        _active_model_key = model_key
        _active_voice_id = voice["id"]


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._")
    return value[:80] or "voice"


def cache_key(request: SynthesizeRequest, voice: dict[str, Any]) -> str:
    inference = inference_params(request, voice)
    payload = json.dumps(
        {
            "voice": voice["id"],
            "text": request.text,
            "speed": request.speed,
            "gpt": voice.get("gpt_model"),
            "sovits": voice.get("sovits_model"),
            "inference": inference,
            # 桌面端每次点击生成都会传入唯一编号，确保同文案也重新抽卡。
            "request_id": request.request_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inference_params(request: SynthesizeRequest, voice: dict[str, Any]) -> dict[str, Any]:
    """返回稳定、可复现的推理参数，并允许请求临时覆盖。"""
    configured = voice.get("inference") or {}
    params: dict[str, Any] = {
        "top_k": int(configured.get("top_k", 15)),
        "top_p": float(configured.get("top_p", 0.9)),
        "temperature": float(configured.get("temperature", 0.8)),
        "sample_steps": int(configured.get("sample_steps", 32)),
        "cut_punc": str(configured.get("cut_punc", "，。？！；：,.?!…")),
    }
    for name in ("top_k", "top_p", "temperature", "sample_steps", "cut_punc"):
        value = getattr(request, name)
        if value is not None:
            params[name] = value
    return params


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "owvoice": True,
        "gpt_sovits_online": engine_online(),
        "gpt_sovits_api": GSV_API,
        "active_voice_id": _active_voice_id,
    }


@app.get("/api/voices")
def voices() -> list[dict[str, Any]]:
    result = []
    for voice in load_voices():
        item = dict(voice)
        item.pop("gpt_model", None)
        item.pop("sovits_model", None)
        item["availability"] = availability(voice)
        result.append(item)
    return result


@app.post("/api/voices/{voice_id}/activate")
def activate(voice_id: str) -> dict[str, str]:
    if not engine_online():
        raise HTTPException(status_code=503, detail="GPT-SoVITS 引擎未在线")
    voice = find_voice(voice_id)
    activate_voice(voice)
    return {"voice_id": voice_id, "status": "active"}


@app.post("/api/synthesize")
def synthesize(request: SynthesizeRequest) -> FileResponse:
    if not engine_online():
        raise HTTPException(status_code=503, detail="GPT-SoVITS 引擎未在线")

    voice = find_voice(request.voice_id)
    reference = resolve_path(voice.get("reference_audio"))
    prompt_text = str(voice.get("prompt_text", "")).strip()
    if not reference or not reference.is_file():
        raise HTTPException(status_code=422, detail=f"参考音频不存在：{reference}")
    if not prompt_text:
        raise HTTPException(status_code=422, detail="角色没有配置参考文字")

    activate_voice(voice)
    # 缓存只供后端复用，不能写入用户的输出目录，避免生成大量哈希文件。
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{cache_key(request, voice)}.wav"
    if not cache_path.exists():
        inference = inference_params(request, voice)
        params = {
            "text": request.text,
            "text_language": voice.get("locale", "zh-CN").split("-")[0],
            "speed": request.speed,
            "refer_wav_path": str(reference),
            "prompt_text": prompt_text,
            "prompt_language": voice.get("prompt_language", "zh"),
            **inference,
        }
        try:
            response = requests.post(f"{GSV_API}/", json=params, timeout=300)
            if response.status_code != 200:
                response = requests.get(f"{GSV_API}/", params=params, timeout=300)
        except requests.exceptions.ChunkedEncodingError as exc:
            raise HTTPException(
                status_code=502,
                detail="GPT-SoVITS 在返回音频前异常中断，请查看 logs\\gpt_sovits.error.log。",
            ) from exc
        except requests.RequestException as exc:
            raise HTTPException(status_code=503, detail=f"语音合成请求失败：{exc}") from exc
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"GPT-SoVITS 合成失败：{response.text}")
        cache_path.write_bytes(response.content)

    filename = f"{safe_filename(voice.get('display_name', request.voice_id))}.wav"
    return FileResponse(cache_path, media_type="audio/wav", filename=filename)
