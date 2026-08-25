"""OwVoice 本地后端：管理人物模型并代理 GPT-SoVITS API。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_DIR / "config"
OUTPUT_DIR = PROJECT_DIR / "outputs"
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


def engine_online() -> bool:
    try:
        response = requests.get(f"{GSV_API}/control", timeout=2)
        return response.status_code in (200, 404, 405)
    except requests.RequestException:
        return False


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

    model_key = f"{gpt_path.resolve()}|{sovits_path.resolve()}"
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
    payload = json.dumps(
        {
            "voice": voice["id"],
            "text": request.text,
            "speed": request.speed,
            "gpt": voice.get("gpt_model"),
            "sovits": voice.get("sovits_model"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OUTPUT_DIR / f"{cache_key(request, voice)}.wav"
    if not cache_path.exists():
        params = {
            "text": request.text,
            "text_language": voice.get("locale", "zh-CN").split("-")[0],
            "speed": request.speed,
            "refer_wav_path": str(reference),
            "prompt_text": prompt_text,
            "prompt_language": voice.get("prompt_language", "zh"),
        }
        try:
            response = requests.post(f"{GSV_API}/", json=params, timeout=300)
            if response.status_code != 200:
                response = requests.get(f"{GSV_API}/", params=params, timeout=300)
        except requests.RequestException as exc:
            raise HTTPException(status_code=503, detail=f"语音合成请求失败：{exc}") from exc
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"GPT-SoVITS 合成失败：{response.text}")
        cache_path.write_bytes(response.content)

    filename = f"{safe_filename(voice.get('display_name', request.voice_id))}_{uuid.uuid4().hex[:8]}.wav"
    return FileResponse(cache_path, media_type="audio/wav", filename=filename)

