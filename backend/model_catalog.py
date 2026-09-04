"""OwVoice 角色模型目录的基础读写工具。

本模块只负责模型注册表和元数据，不负责下载或删除文件，便于后续接入模型广场时单独测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
REGISTRY_RELATIVE_PATH = Path("models") / "installed-models.json"
MODEL_ROOT_RELATIVE_PATH = Path("models")
# 兼容旧代码名称；实际目录不再包含 characters 层级。
CHARACTER_ROOT_RELATIVE_PATH = MODEL_ROOT_RELATIVE_PATH


class ModelCatalogError(ValueError):
    """模型注册信息不符合规范。"""


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

    registry_path = get_registry_path(project_dir)
    if not registry_path.exists():
        return []
    try:
        # Windows PowerShell 5 的 UTF-8 文件可能带 BOM，需兼容读取。
        payload = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ModelCatalogError("installed-models.json 顶层必须是对象")
        values = payload.get("models", [])
        if not isinstance(values, list):
            raise ModelCatalogError("installed-models.json 的 models 必须是数组")
        return [InstalledModel.from_dict(value) for value in values]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelCatalogError(f"无法读取模型注册表：{registry_path}") from exc


def save_installed_models(project_dir: str | Path, models: list[InstalledModel]) -> None:
    """以临时文件加替换方式保存，避免程序中断造成注册表半文件。"""

    registry_path = get_registry_path(project_dir)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "models": [model.to_dict() for model in models]}
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=registry_path.parent,
        prefix="installed-models.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(registry_path)


def register_installed_model(project_dir: str | Path, model: InstalledModel) -> None:
    """登记一个已完成校验并安装到本地的模型。"""

    validate_model_id(model.id)
    models = [item for item in load_installed_models(project_dir) if item.id != model.id]
    models.append(model)
    models.sort(key=lambda item: item.id.casefold())
    save_installed_models(project_dir, models)
