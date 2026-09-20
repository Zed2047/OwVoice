"""OwVoice 角色模型目录的基础读写工具。

本模块只负责模型注册表和元数据，不负责下载或删除文件，便于后续接入模型广场时单独测试。
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import stat
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.atomic_json import write_json_atomic
from backend.text_encoding import read_text_compat


MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
REGISTRY_RELATIVE_PATH = Path("models") / "installed-models.json"
MODEL_ROOT_RELATIVE_PATH = Path("models")
# 兼容旧代码名称；实际目录不再包含 characters 层级。
CHARACTER_ROOT_RELATIVE_PATH = MODEL_ROOT_RELATIVE_PATH

_CATALOG_LOCKS_GUARD = threading.Lock()
_CATALOG_LOCKS: dict[str, threading.RLock] = {}


class ModelCatalogError(ValueError):
    """模型注册信息不符合规范。"""


def get_model_catalog_lock(project_dir: str | Path) -> threading.RLock:
    """返回项目级共享锁，串行化同一模型库的读改写事务。"""

    key = str(Path(project_dir).resolve()).casefold()
    with _CATALOG_LOCKS_GUARD:
        lock = _CATALOG_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _CATALOG_LOCKS[key] = lock
        return lock


@dataclass(frozen=True)
class InstalledModel:
    """本地已安装角色模型的最小信息。"""

    id: str
    name: str
    version: str
    path: str
    files: list[str] = field(default_factory=list)
    sha256: str = ""
    min_app_version: str = "0.0.0"
    min_engine_version: str = "0.0.0"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstalledModel":
        if not isinstance(value, dict):
            raise ModelCatalogError("模型注册项必须是对象")
        model_id = str(value.get("id", ""))
        validate_model_id(model_id)
        path = str(value.get("path", ""))
        if not path:
            raise ModelCatalogError(f"模型 {model_id} 缺少 path")
        path_value = Path(path)
        if path_value.is_absolute() or ".." in path_value.parts:
            raise ModelCatalogError(f"模型 {model_id} 的 path 非法")
        files = value.get("files", [])
        if not isinstance(files, list):
            raise ModelCatalogError(f"模型 {model_id} 的 files 必须是数组")
        return cls(
            id=model_id,
            name=str(value.get("name", model_id)),
            version=str(value.get("version", "0.0.0")),
            path=path,
            files=[str(item) for item in files],
            sha256=str(value.get("sha256", "")),
            min_app_version=str(value.get("minAppVersion", "0.0.0")),
            min_engine_version=str(value.get("minEngineVersion", "0.0.0")),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["minAppVersion"] = value.pop("min_app_version")
        value["minEngineVersion"] = value.pop("min_engine_version")
        return value


def validate_model_id(model_id: str) -> None:
    """拒绝可能造成路径穿越或目录歧义的模型 ID。"""

    if not MODEL_ID_PATTERN.fullmatch(model_id):
        raise ModelCatalogError(f"非法模型 ID：{model_id!r}")


def get_default_data_dir(project_dir: str | Path) -> Path:
    """返回用户数据目录，避免更新或重新解压覆盖用户模型。"""

    # 数据随项目目录保存；更新脚本不触碰 data，因此不会覆盖用户模型。
    return Path(project_dir).resolve() / "data"


def get_registry_path(
    project_dir: str | Path,
    data_dir: str | Path | None = None,
) -> Path:
    root = Path(data_dir) if data_dir is not None else get_default_data_dir(project_dir)
    return root / Path("models") / "installed-models.json"


def get_character_root(
    project_dir: str | Path,
    data_dir: str | Path | None = None,
) -> Path:
    root = Path(data_dir) if data_dir is not None else get_default_data_dir(project_dir)
    return root / Path("models")


def load_installed_models(project_dir: str | Path) -> list[InstalledModel]:
    """读取本地注册表；首次运行或文件损坏时给出明确异常。"""

    with get_model_catalog_lock(project_dir):
        registry_path = get_registry_path(project_dir)
        if not registry_path.exists():
            return []
        try:
            # Windows PowerShell 5 的 UTF-8 文件可能带 BOM，需兼容读取。
            payload = json.loads(read_text_compat(registry_path))
            if not isinstance(payload, dict):
                raise ModelCatalogError("installed-models.json 顶层必须是对象")
            values = payload.get("models", [])
            if not isinstance(values, list):
                raise ModelCatalogError("installed-models.json 的 models 必须是数组")
            models: list[InstalledModel] = []
            changed = False
            project_root = Path(project_dir).resolve()
            current_model_root = project_root / "data" / "models"
            for value in values:
                if not isinstance(value, dict):
                    raise ModelCatalogError("模型注册项必须是对象")
                candidate = dict(value)
                raw_path = str(candidate.get("path", ""))
                # 旧版本曾保存绝对路径。项目移动后，只能把确实已经随项目
                # 移到当前 data/models/<id> 的目录安全映射回来；外部路径不自动信任。
                if raw_path and Path(raw_path).is_absolute():
                    model_id = str(candidate.get("id", ""))
                    validate_model_id(model_id)
                    old_name = Path(raw_path).name
                    local_candidates = []
                    for directory_name in (old_name, model_id):
                        local_target = current_model_root / directory_name
                        if local_target not in local_candidates:
                            local_candidates.append(local_target)
                    matched_target = None
                    for local_target in local_candidates:
                        try:
                            local_metadata = json.loads(
                                read_text_compat(local_target / "model.json")
                            )
                        except (OSError, UnicodeError, json.JSONDecodeError):
                            continue
                        if isinstance(local_metadata, dict) and str(local_metadata.get("id", model_id)) == model_id:
                            matched_target = local_target
                            break
                    if matched_target is not None:
                        candidate["path"] = str(Path("models") / matched_target.name).replace("\\", "/")
                        changed = True
                    else:
                        # 外部旧路径无法证明属于当前项目，忽略该条登记，保留文件不动。
                        changed = True
                        continue
                models.append(InstalledModel.from_dict(candidate))
            if changed:
                save_installed_models(project_dir, models)
            return models
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelCatalogError(f"无法读取模型注册表：{registry_path}") from exc


def save_installed_models(project_dir: str | Path, models: list[InstalledModel]) -> None:
    """以临时文件加替换方式保存，避免程序中断造成注册表半文件。"""

    with get_model_catalog_lock(project_dir):
        registry_path = get_registry_path(project_dir)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "models": [model.to_dict() for model in models]}
        write_json_atomic(registry_path, payload)


def register_installed_model(project_dir: str | Path, model: InstalledModel) -> None:
    """登记一个已完成校验并安装到本地的模型。"""

    with get_model_catalog_lock(project_dir):
        validate_model_id(model.id)
        models = [item for item in load_installed_models(project_dir) if item.id != model.id]
        models.append(model)
        models.sort(key=lambda item: item.id.casefold())
        save_installed_models(project_dir, models)


def _recovery_model(project_root: Path, directory: Path) -> InstalledModel:
    if directory.is_symlink():
        raise ModelCatalogError("模型目录不能是链接")
    metadata = json.loads(read_text_compat(directory / "model.json"))
    if not isinstance(metadata, dict):
        raise ModelCatalogError("model.json 顶层必须是对象")
    model_id = str(metadata.get("id", "")).strip()
    validate_model_id(model_id)
    for field in ("gpt_model", "sovits_model", "reference_audio"):
        relative = Path(str(metadata.get(field, "")).strip())
        if not relative.parts or relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
            raise ModelCatalogError(f"{field} 路径无效")
        resolved = (directory / relative).resolve()
        if directory.resolve() not in resolved.parents or not resolved.is_file():
            raise ModelCatalogError(f"{field} 文件不存在或路径越界")
    files = sorted(
        str(item.relative_to(directory)).replace("\\", "/")
        for item in directory.rglob("*")
        if item.is_file() and not item.is_symlink()
    )
    return InstalledModel(
        id=model_id,
        name=str(metadata.get("display_name") or metadata.get("name") or model_id),
        version=str(metadata.get("version", "0.0.0")),
        path=str(Path("models") / directory.name).replace("\\", "/"),
        files=files,
    )


def _catalog_recovery_candidates(project_dir: str | Path) -> tuple[list[InstalledModel], list[dict[str, str]]]:
    project_root = Path(project_dir).resolve()
    model_root = get_character_root(project_root)
    candidates: list[InstalledModel] = []
    rejected: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if not model_root.is_dir():
        return candidates, rejected
    for directory in sorted(model_root.iterdir(), key=lambda item: item.name.casefold()):
        if not directory.is_dir() or directory.name == "characters" or not (directory / "model.json").is_file():
            continue
        try:
            model = _recovery_model(project_root, directory)
            if model.id in seen_ids:
                raise ModelCatalogError(f"模型 ID 重复：{model.id}")
            seen_ids.add(model.id)
            candidates.append(model)
        except (OSError, UnicodeError, json.JSONDecodeError, ModelCatalogError) as exc:
            rejected.append({"directory": directory.name, "error": str(exc)})
    return candidates, rejected


def _preserve_corrupt_registry(project_dir: str | Path) -> Path:
    project_root = Path(project_dir).resolve()
    registry_path = get_registry_path(project_root)
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()[:12]
    diagnostic = registry_path.with_name(f"installed-models.corrupt-{digest}.json")
    if not diagnostic.exists():
        shutil.copy2(registry_path, diagnostic)
        diagnostic.chmod(stat.S_IREAD)
    return diagnostic


def get_catalog_recovery_status(project_dir: str | Path) -> dict[str, Any]:
    """损坏时保留原件并返回只读恢复预览，不修改注册表。"""

    project_root = Path(project_dir).resolve()
    try:
        models = load_installed_models(project_root)
        return {
            "needs_recovery": False,
            "candidates": [model.to_dict() for model in models],
            "rejected": [],
            "diagnostic_path": None,
        }
    except ModelCatalogError as exc:
        diagnostic = _preserve_corrupt_registry(project_root)
        candidates, rejected = _catalog_recovery_candidates(project_root)
        return {
            "needs_recovery": True,
            "error": str(exc),
            "candidates": [model.to_dict() for model in candidates],
            "rejected": rejected,
            "diagnostic_path": str(diagnostic.relative_to(project_root)).replace("\\", "/"),
        }


def recover_installed_models(project_dir: str | Path, *, confirmed: bool) -> list[InstalledModel]:
    """仅在用户明确确认后，用合法 model.json 重建损坏注册表。"""

    if not confirmed:
        raise ModelCatalogError("重建模型注册表需要用户明确确认")
    project_root = Path(project_dir).resolve()
    status = get_catalog_recovery_status(project_root)
    if not status["needs_recovery"]:
        return load_installed_models(project_root)
    candidates, _rejected = _catalog_recovery_candidates(project_root)
    if not candidates:
        raise ModelCatalogError("没有找到可恢复的合法模型，损坏注册表已保留")
    candidates.sort(key=lambda item: item.id.casefold())
    save_installed_models(project_root, candidates)
    return candidates
