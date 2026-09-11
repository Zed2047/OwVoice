"""OwVoice 本地后端：管理人物模型并代理 GPT-SoVITS API。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from functools import wraps
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from backend.model_catalog import ModelCatalogError, get_character_root, load_installed_models
from backend.model_manager import ModelManager, ModelManagerError
from backend.training_errors import TrainingError
from backend.update_manager import UpdateManager, UpdateManagerError


PROJECT_DIR = Path(os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1]))
CONFIG_DIR = PROJECT_DIR / "config"
CACHE_DIR = PROJECT_DIR / ".cache" / "synthesis"
CONFIG_PATH = Path(os.environ.get("OWVOICE_CONFIG", CONFIG_DIR / "voices.local.json"))
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = PROJECT_DIR / CONFIG_PATH
GSV_API = os.environ.get("OWVOICE_GSV_API", "http://127.0.0.1:9880").rstrip("/")
MODEL_MANAGER = ModelManager(PROJECT_DIR, os.environ.get("OWVOICE_MODEL_INDEX_URL"))
UPDATE_MANAGER = UpdateManager(os.environ.get("OWVOICE_APP_VERSION", "0.2.0"))

app = FastAPI(title="OwVoice API", version="0.2.0")
_model_lock = threading.Lock()
_synthesis_lock = threading.Lock()
_active_model_key: str | None = None
_active_voice_id: str | None = None
_selected_voice_id: str | None = None


def _training_manager():
    """按需加载训练管理器，避免普通启动携带训练依赖。"""

    from backend.training import TRAINING_MANAGER

    return TRAINING_MANAGER


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


class TrainingCreateRequest(BaseModel):
    """桌面端创建本地训练任务。音频路径只在本机桌面端解析。"""

    name: str = Field(min_length=1, max_length=80)
    language: str = Field(default="zh", min_length=2, max_length=8)
    source_paths: list[str] = Field(min_length=1, max_length=200)
    avatar_path: str | None = Field(default=None, max_length=1000)


class TrainingAudioRequest(BaseModel):
    source_paths: list[str] = Field(min_length=1, max_length=200)


class TrainingRemoveFilesRequest(BaseModel):
    file_names: list[str] = Field(min_length=1, max_length=200)


class TrainingImageRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=1000)


class TrainingSessionHeartbeatRequest(BaseModel):
    job_id: str | None = Field(default=None, max_length=80)


class TrainingTranscriptItem(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=2000)


class TrainingTranscriptRequest(BaseModel):
    transcripts: list[TrainingTranscriptItem] = Field(min_length=1, max_length=200)


class ModelRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def load_voices() -> list[dict[str, Any]]:
    if not CONFIG_PATH.exists():
        # 空模型库是合法的首次启动状态；用户可以之后导入自己的模型。
        return []
    # 本地模型库注册表是唯一角色来源；voices.local.json 只保留应用配置，
    # 避免删除模型后又被旧配置重新加载。
    installed_voices: list[dict[str, Any]] = []
    character_root = get_character_root(PROJECT_DIR).resolve()
    for installed in load_installed_models(PROJECT_DIR):
        model_path = Path(installed.path)
        model_root = (
            model_path.resolve()
            if model_path.is_absolute()
            else (character_root / model_path.name).resolve()
        )
        if character_root not in model_root.parents:
            continue
        metadata_path = model_root / "model.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or str(metadata.get("id", installed.id)) != installed.id:
            continue

        voice = dict(metadata)
        voice["id"] = installed.id
        voice["display_name"] = str(
            voice.get("display_name") or voice.get("name") or installed.name or installed.id
        )
        voice["enabled"] = bool(voice.get("enabled", True))
        voice["model_version"] = installed.version
        voice["model_source"] = "local"
        for field in ("gpt_model", "sovits_model", "reference_audio", "avatar"):
            value = str(voice.get(field, "")).strip()
            if value and not Path(value).is_absolute():
                resolved = (model_root / value).resolve()
                if model_root not in resolved.parents:
                    voice[field] = ""
                else:
                    voice[field] = str(resolved.relative_to(PROJECT_DIR)).replace("\\", "/")
        installed_voices.append(voice)

    return [voice for voice in installed_voices if voice.get("enabled", True)]


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
    global _active_model_key, _active_voice_id, _selected_voice_id

    initial_voice_id = os.environ.get("OWVOICE_INITIAL_VOICE_ID", "").strip()
    if not initial_voice_id:
        return
    try:
        voice = next((item for item in load_voices() if item.get("id") == initial_voice_id), None)
        model_key = model_key_for_voice(voice) if voice else None
        if voice and model_key:
            _active_model_key = model_key
            _active_voice_id = initial_voice_id
            _selected_voice_id = initial_voice_id
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, ModelCatalogError):
        # 启动器会在真正合成前重新校验模型；这里不能阻止后端启动。
        return


def engine_online() -> bool:
    try:
        # 9880 未启动时无需阻塞整个 /api/health；健康接口本身仍应快速返回。
        response = requests.get(f"{GSV_API}/control", timeout=0.5)
        return response.status_code in (200, 404, 405)
    except requests.RequestException:
        return False


def _same_path(left: object, right: object) -> bool:
    """比较 Windows 路径，避免大小写或斜杠差异造成误判。"""
    if not left or not right:
        return False
    try:
        left_path = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(left))))
        right_path = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(right))))
    except (TypeError, ValueError, OSError):
        return False
    return left_path == right_path


def engine_model_ready(gpt_path: Path, sovits_path: Path) -> bool:
    """确认 GPT-SoVITS 进程中确实加载了当前这组权重。"""
    try:
        response = requests.get(f"{GSV_API}/model_status", timeout=3)
    except requests.RequestException:
        return False
    if response.status_code != 200:
        # 旧版引擎没有该接口时按未确认处理，后续会重新发送 set_model。
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(payload.get("loaded")) and _same_path(payload.get("gpt_model_path"), gpt_path) and _same_path(
        payload.get("sovits_model_path"), sovits_path
    )


initialize_active_voice_from_environment()


def activate_voice(voice: dict[str, Any]) -> None:
    global _active_model_key, _active_voice_id, _selected_voice_id

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
        if model_key == _active_model_key and engine_model_ready(gpt_path, sovits_path):
            _active_voice_id = voice["id"]
            _selected_voice_id = voice["id"]
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
        _selected_voice_id = voice["id"]


def _serialize_synthesis(func):
    """让卸载操作等待当前合成结束，避免推理过程中清空模型对象。"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        with _synthesis_lock:
            return func(*args, **kwargs)

    return wrapper


def release_active_model() -> dict[str, str]:
    """卸载 GPT-SoVITS 当前权重，保留两个本地服务进程。"""

    global _active_model_key, _active_voice_id
    with _synthesis_lock:
        if engine_online():
            try:
                response = requests.post(f"{GSV_API}/unload_model", timeout=120)
            except requests.RequestException as exc:
                raise HTTPException(status_code=503, detail=f"卸载模型请求失败：{exc}") from exc
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail=f"GPT-SoVITS 模型卸载失败：{response.text}")
        with _model_lock:
            _active_model_key = None
            _active_voice_id = None
    return {"status": "unloaded"}


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


def _valid_wav_file(path: Path) -> bool:
    """只接受完整的 RIFF/WAVE 缓存，避免复用上次中断留下的错误响应。"""

    try:
        with path.open("rb") as handle:
            header = handle.read(44)
        return len(header) == 44 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    except OSError:
        return False


def inference_params(request: SynthesizeRequest, voice: dict[str, Any]) -> dict[str, Any]:
    """返回稳定、可复现的推理参数，并允许请求临时覆盖。"""
    configured = voice.get("inference") or {}
    if not isinstance(configured, dict):
        configured = {}
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
        "project_dir": str(PROJECT_DIR),
        "gpt_sovits_online": engine_online(),
        "gpt_sovits_api": GSV_API,
        "active_voice_id": _active_voice_id,
        "selected_voice_id": _selected_voice_id,
    }


@app.post("/api/training/session/heartbeat")
def training_session_heartbeat(
    request: TrainingSessionHeartbeatRequest,
    x_owvoice_session: str = Header(default=""),
) -> dict[str, Any]:
    try:
        return _training_manager().heartbeat(x_owvoice_session, request.job_id)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc



@app.get("/api/updates")
def check_updates() -> dict[str, Any]:
    try:
        return UPDATE_MANAGER.check()
    except UpdateManagerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
@app.get("/api/models")
def models(refresh: bool = False) -> list[dict[str, Any]]:
    """返回本地模型清单；公开版不联网，也不提供远程模型下载。"""

    try:
        return MODEL_MANAGER.list_models(refresh=refresh)
    except ModelCatalogError as exc:
        raise HTTPException(status_code=500, detail=f"无法读取本地模型注册表：{exc}") from exc


@app.post("/api/models/refresh")
def refresh_models() -> dict[str, Any]:
    try:
        return MODEL_MANAGER.refresh_catalog()
    except ModelManagerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/models/{model_id}/install")
def install_model(model_id: str) -> dict[str, Any]:
    try:
        return {"status": "installed", "model": MODEL_MANAGER.install_model(model_id)}
    except ModelManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/models/{model_id}/update")
def update_model(model_id: str) -> dict[str, Any]:
    try:
        return {"status": "updated", "model": MODEL_MANAGER.update_model(model_id, active_model_id=_active_voice_id)}
    except ModelManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
@app.delete("/api/models/{model_id}")
def delete_model(model_id: str) -> dict[str, str]:
    global _selected_voice_id
    try:
        MODEL_MANAGER.remove_model(model_id, active_model_id=_active_voice_id)
    except ModelManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _selected_voice_id == model_id:
        _selected_voice_id = None
    return {"status": "deleted", "model_id": model_id}


@app.post("/api/models/{model_id}/rename")
def rename_model(model_id: str, request: ModelRenameRequest) -> dict[str, Any]:
    try:
        model = MODEL_MANAGER.rename_model(model_id, request.name, active_model_id=_active_voice_id)
    except ModelManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "renamed", "model": model}
@app.get("/api/voices")
def voices() -> list[dict[str, Any]]:
    # API 只监听本机；桌面端需要这些相对路径在无模型启动后拉起引擎。
    try:
        result = []
        for voice in load_voices():
            item = dict(voice)
            item["availability"] = availability(voice)
            result.append(item)
        return result
    except ModelCatalogError as exc:
        raise HTTPException(status_code=500, detail=f"无法读取本地模型注册表：{exc}") from exc


@app.post("/api/voices/{voice_id}/select")
def select_voice(voice_id: str) -> dict[str, str]:
    """记录桌面端当前选择的角色，不立即触发昂贵的引擎换模。"""

    global _selected_voice_id
    voice = find_voice(voice_id)
    required = (
        resolve_path(voice.get("gpt_model")),
        resolve_path(voice.get("sovits_model")),
        resolve_path(voice.get("reference_audio")),
    )
    if any(path is None or not path.is_file() for path in required):
        raise HTTPException(status_code=422, detail="角色模型或参考音频不存在")
    _selected_voice_id = voice_id
    return {"voice_id": voice_id, "status": "selected"}


@app.post("/api/voices/{voice_id}/activate")
def activate(voice_id: str) -> dict[str, str]:
    if not engine_online():
        raise HTTPException(status_code=503, detail="GPT-SoVITS 引擎未在线")
    voice = find_voice(voice_id)
    # 角色切换不能与推理或卸载并发，否则 GPT-SoVITS 可能在 speaker_list
    # 被清空的窗口收到请求，表现为 KeyError: 'default'。
    with _synthesis_lock:
        activate_voice(voice)
    return {"voice_id": voice_id, "status": "active"}


@app.post("/api/engine/unload")
def unload_engine() -> dict[str, str]:
    return release_active_model()


@app.post("/api/synthesize")
@_serialize_synthesis
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
    if cache_path.exists() and not _valid_wav_file(cache_path):
        # 该目录只保存后端自动生成的缓存；损坏缓存可以安全重建。
        try:
            cache_path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法清理损坏的音频缓存：{exc}") from exc
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
        content = response.content
        if len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
            raise HTTPException(status_code=502, detail="GPT-SoVITS 返回的不是有效 WAV 音频")
        temporary_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        try:
            temporary_path.write_bytes(content)
            temporary_path.replace(cache_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"保存合成音频失败：{exc}") from exc
        finally:
            temporary_path.unlink(missing_ok=True)

    filename = f"{safe_filename(voice.get('display_name', request.voice_id))}.wav"
    return FileResponse(cache_path, media_type="audio/wav", filename=filename)
class LocalModelImportRequest(BaseModel):
    """本地目录导入请求；路径只在桌面端本机解析。"""

    source_dir: str = Field(min_length=1, max_length=1000)


@app.post("/api/models/import")
def import_local_model(request: LocalModelImportRequest) -> dict[str, Any]:
    try:
        result = MODEL_MANAGER.import_models(request.source_dir)
    except ModelManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result["imported"] and not result["skipped"]:
        details = "\n".join(f"{item['source']}：{item['error']}" for item in result["failed"])
        raise HTTPException(status_code=400, detail=f"没有成功导入任何模型。\n{details}")
    return {
        "status": "imported",
        "model": result["imported"][0] if len(result["imported"]) == 1 else None,
        "models": result["imported"],
        "skipped": result["skipped"],
        "failed": result["failed"],
    }


@app.post("/api/training/jobs")
def create_training_job(request: TrainingCreateRequest) -> dict[str, Any]:
    try:
        return _training_manager().create_job(
            request.name,
            request.language,
            request.source_paths,
            request.avatar_path,
        )
    except TrainingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/training/environment")
def get_training_environment() -> dict[str, Any]:
    return _training_manager().environment_status()


@app.get("/api/training/jobs/recent")
def get_recent_training_job() -> dict[str, Any]:
    return {"job": _training_manager().latest_recoverable_job()}


@app.post("/api/training/jobs/{job_id}/files")
def add_training_files(job_id: str, request: TrainingAudioRequest) -> dict[str, Any]:
    try:
        return _training_manager().add_files(job_id, request.source_paths)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/training/jobs/{job_id}/files")
def clear_training_files(job_id: str) -> dict[str, Any]:
    try:
        return _training_manager().clear_files(job_id)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/files/remove")
def remove_training_files(job_id: str, request: TrainingRemoveFilesRequest) -> dict[str, Any]:
    try:
        return _training_manager().remove_files(job_id, request.file_names)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/training/jobs/{job_id}/files/{file_name:path}")
def remove_training_file(job_id: str, file_name: str) -> dict[str, Any]:
    try:
        return _training_manager().remove_file(job_id, file_name)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/avatar")
def set_training_avatar(job_id: str, request: TrainingImageRequest) -> dict[str, Any]:
    try:
        return _training_manager().set_avatar(job_id, request.source_path)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/training/jobs/{job_id}")
def get_training_job(job_id: str) -> dict[str, Any]:
    try:
        return _training_manager().get_job(job_id)
    except TrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/training/jobs/{job_id}/avatar")
def preview_training_avatar(job_id: str) -> FileResponse:
    try:
        path = _training_manager().avatar_file_path(job_id)
    except TrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get("/api/training/jobs/{job_id}/log")
def get_training_log(job_id: str) -> dict[str, str]:
    try:
        return {"log": _training_manager().log_text(job_id)}
    except TrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/training/jobs/{job_id}/files/{file_name:path}/audio")
def preview_training_file(job_id: str, file_name: str) -> FileResponse:
    try:
        path = _training_manager().file_path(job_id, file_name)
    except TrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.put("/api/training/jobs/{job_id}/transcripts")
def update_training_transcripts(job_id: str, request: TrainingTranscriptRequest) -> dict[str, Any]:
    try:
        return _training_manager().update_transcripts(
            job_id,
            [item.model_dump() for item in request.transcripts],
        )
    except TrainingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/transcribe")
def transcribe_training_job(job_id: str, x_owvoice_session: str = Header(default="")) -> dict[str, Any]:
    try:
        return _training_manager().start_transcription(job_id, x_owvoice_session)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/prepare")
def prepare_training_job(
    job_id: str,
    request: TrainingTranscriptRequest | None = None,
    x_owvoice_session: str = Header(default=""),
) -> dict[str, Any]:
    try:
        if request is not None:
            return _training_manager().prepare_with_transcripts(
                job_id,
                [item.model_dump() for item in request.transcripts],
                x_owvoice_session,
            )
        return _training_manager().start_prepare(job_id, x_owvoice_session)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/start")
def start_training_job(job_id: str, x_owvoice_session: str = Header(default="")) -> dict[str, Any]:
    try:
        return _training_manager().start_training(job_id, x_owvoice_session)
    except TrainingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/cancel")
def cancel_training_job(job_id: str, x_owvoice_session: str = Header(default="")) -> dict[str, Any]:
    try:
        return _training_manager().cancel(job_id, x_owvoice_session)
    except TrainingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/training/jobs/{job_id}/finalize")
def finalize_training_job(job_id: str) -> dict[str, Any]:
    try:
        return _training_manager().finalize(job_id)
    except TrainingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/training/status")
def training_status() -> dict[str, Any]:
    return {
        "enabled": True,
        "message": "本地训练向导已启用；训练在本机执行。",
    }

initialize_active_voice_from_environment()
