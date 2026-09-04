"""OwVoice 本地模型库管理。公开版不提供联网模型下载。"""

from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from backend.model_catalog import (
    InstalledModel,
    ModelCatalogError,
    get_character_root,
    load_installed_models,
    register_installed_model,
    save_installed_models,
    validate_model_id,
)


class ModelManagerError(RuntimeError):
    """模型操作失败。"""


class ModelManager:
    """只管理用户本地导入或本地训练得到的模型。"""

    FORBIDDEN_EXTENSIONS = {".exe", ".dll", ".py", ".pyc", ".ps1", ".bat", ".cmd", ".com", ".scr", ".sh"}


    def __init__(self, project_dir: str | Path, index_url: str | None = None) -> None:
        del index_url
        self.project_dir = Path(project_dir).resolve()
        self.character_root = get_character_root(self.project_dir)
        self._migrate_legacy_model_dirs()

    def _model_root(self, model: InstalledModel) -> Path:
        path = Path(model.path)
        return (path if path.is_absolute() else self.character_root / path).resolve()

    def _validate_metadata(self, metadata: object, model_id: str, model_root: Path) -> dict[str, Any]:
        if not isinstance(metadata, dict) or str(metadata.get("id", model_id)) != model_id:
            raise ModelManagerError("model.json 的 id 无效")
        root = model_root.resolve()
        for field in ("gpt_model", "sovits_model", "reference_audio"):
            value = str(metadata.get(field, "")).strip()
            if not value or Path(value).is_absolute() or ":" in value:
                raise ModelManagerError(f"model.json 缺少有效的 {field}")
            resolved = (root / value).resolve()
            if root not in resolved.parents or not resolved.is_file():
                raise ModelManagerError(f"模型文件不存在或路径非法：{field}")
        inference = metadata.get("inference", {})
        if inference is not None and not isinstance(inference, dict):
            raise ModelManagerError("model.json 的 inference 必须是对象")
        avatar = str(metadata.get("avatar", "")).strip()
        if avatar:
            resolved = (root / avatar).resolve()
            if root not in resolved.parents or not resolved.is_file():
                raise ModelManagerError("model.json 的 avatar 路径无效")
        return metadata

    def _validate_package_files(self, source: Path) -> None:
        """拒绝模型包夹带可执行脚本或通过链接逃逸目录。"""
        for item in source.rglob("*"):
            if item.is_symlink():
                raise ModelManagerError(f"模型包不允许包含符号链接：{item}")
            if item.is_file() and item.suffix.casefold() in self.FORBIDDEN_EXTENSIONS:
                raise ModelManagerError(f"模型包包含禁止导入的文件类型：{item.name}")

    def list_models(self, refresh: bool = False) -> list[dict[str, Any]]:
        del refresh
        result: list[dict[str, Any]] = []
        for model in load_installed_models(self.project_dir):
            root = self._model_root(model)
            try:
                size = (
                    sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
                    if root.is_dir()
                    else 0
                )
            except OSError:
                # 单个模型目录权限异常或正在被替换时，不应拖垮整个模型库页面。
                size = 0
            item = {
                **model.to_dict(),
                "installed": root.is_dir(),
                "installedVersion": model.version,
                "size": size,
                "source": "local",
            }
            try:
                metadata = json.loads((root / "model.json").read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                item.update(metadata)
                item["id"] = model.id
                item["display_name"] = str(metadata.get("display_name") or metadata.get("name") or model.name or model.id)
            result.append(item)
        return result

    def refresh_catalog(self) -> dict[str, Any]:
        """兼容旧接口；公开版只返回本地模型，不联网。"""
        return {"models": self.list_models()}

    def import_model(self, source_dir: str | Path) -> dict[str, Any]:
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise ModelManagerError(f"模型目录不存在：{source}")
        if not (source / "model.json").is_file():
            if source == self.character_root:
                raise ModelManagerError("不能把模型文件直接放在 data\\models 根目录，请选择单个模型目录或模型集合目录")
            raise ModelManagerError("请选择包含 model.json 的单个模型目录")
        try:
            metadata = json.loads((source / "model.json").read_text(encoding="utf-8-sig"))
            model_id = str(metadata.get("id", "")).strip()
            validate_model_id(model_id)
            self._validate_package_files(source)
            self._validate_metadata(metadata, model_id, source)
            metadata_changed = False
            if not str(metadata.get("avatar", "")).strip():
                for candidate in ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp", "avatar.svg"):
                    if (source / candidate).is_file():
                        metadata["avatar"] = candidate
                        metadata_changed = True
                        break
                else:
                    metadata["avatar"] = "avatar.svg"
                    metadata_changed = True
        except (OSError, json.JSONDecodeError, ModelCatalogError) as exc:
            raise ModelManagerError(f"模型元数据无效：{exc}") from exc
        target = self.character_root / model_id
        if target.exists():
            registered = next((item for item in load_installed_models(self.project_dir) if item.id == model_id), None)
            if registered is not None:
                raise ModelManagerError(f"模型已经存在：{model_id}")
            if source != target.resolve():
                raise ModelManagerError(f"目标模型目录已存在但尚未登记：{target}")
            existing_metadata_path = target / "model.json"
            try:
                existing_metadata = json.loads(existing_metadata_path.read_text(encoding="utf-8-sig"))
                self._validate_metadata(existing_metadata, model_id, target)
            except (OSError, json.JSONDecodeError, ModelCatalogError, ModelManagerError) as exc:
                raise ModelManagerError(f"模型目录已存在但未登记，且内容不完整：{target}") from exc
            files = [str(item.relative_to(target)).replace("\\", "/") for item in target.rglob("*") if item.is_file()]
            restored = InstalledModel(
                id=model_id,
                name=str(existing_metadata.get("name") or existing_metadata.get("display_name") or model_id),
                version=str(existing_metadata.get("version", "0.0.0")),
                path=str(Path("models") / model_id).replace("\\", "/"),
                files=sorted(files),
            )
            register_installed_model(self.project_dir, restored)
            return {**restored.to_dict(), **existing_metadata, "installed": True, "source": "local"}
        self.character_root.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(tempfile.mkdtemp(prefix="owvoice-model-staging-", dir=self.character_root))
        staging = staging_parent / model_id
        try:
            shutil.copytree(source, staging)
            if metadata_changed:
                if metadata.get("avatar") == "avatar.svg":
                    _ow_write_default_avatar(staging / "avatar.svg", metadata.get("name", model_id))
                (staging / "model.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            staging.rename(target)
            files = [str(item.relative_to(target)).replace("\\", "/") for item in target.rglob("*") if item.is_file()]
            model = InstalledModel(
                id=model_id,
                name=str(metadata.get("name", model_id)),
                version=str(metadata.get("version", "0.0.0")),
                path=str(Path("models") / model_id).replace("\\", "/"),
                files=sorted(files),
            )
            register_installed_model(self.project_dir, model)
            return {**model.to_dict(), **metadata, "installed": True, "source": "local"}
        except (OSError, ModelCatalogError) as exc:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise ModelManagerError(f"导入模型失败：{exc}") from exc
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    def _discover_model_sources(self, source_dir: str | Path) -> list[Path]:
        """识别单个模型包，或集合目录下的一级模型包目录。"""
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise ModelManagerError(f"模型目录不存在：{source}")
        if (source / "model.json").is_file():
            if source == self.character_root:
                raise ModelManagerError(
                    "不能把模型文件直接放在 data\\models 根目录，请将每个模型放入独立文件夹"
                )
            return [source]
        try:
            children = sorted(
                (
                    item
                    for item in source.iterdir()
                    if item.is_dir() and (item / "model.json").is_file()
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            raise ModelManagerError(f"无法读取模型集合目录：{source}\n{exc}") from exc
        if not children:
            raise ModelManagerError(
                "未找到模型包：请选择包含 model.json 的单个模型目录，或包含多个模型目录的集合文件夹"
            )
        return children

    def import_models(self, source_dir: str | Path) -> dict[str, Any]:
        """导入单个模型包或集合目录；每个模型独立校验和提交。"""
        sources = self._discover_model_sources(source_dir)
        registered_ids = {item.id for item in load_installed_models(self.project_dir)}
        imported: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for source in sources:
            # 选择 data/models 作为集合目录时，已登记的规范模型直接跳过。
            if source.parent == self.character_root and source.name in registered_ids:
                continue
            try:
                imported.append(self.import_model(source))
            except ModelManagerError as exc:
                failed.append({"source": str(source), "error": str(exc)})
        return {"imported": imported, "failed": failed}

    def install_model(self, model_id: str) -> dict[str, Any]:
        del model_id
        raise ModelManagerError("OwVoice 已停止联网模型下载，请先导入本地模型")

    def update_model(self, model_id: str, active_model_id: str | None = None) -> dict[str, Any]:
        del model_id, active_model_id
        raise ModelManagerError("OwVoice 不提供模型联网更新，请导入或重新训练本地模型")

    def remove_model(self, model_id: str, active_model_id: str | None = None) -> None:
        validate_model_id(model_id)
        if active_model_id == model_id:
            raise ModelManagerError("当前正在使用该角色，请先切换角色后再删除")
        models = load_installed_models(self.project_dir)
        model = next((item for item in models if item.id == model_id), None)
        if model is None:
            raise ModelManagerError(f"模型未安装：{model_id}")
        model_path = self._model_root(model)
        root = self.character_root.resolve()
        if root not in model_path.parents:
            raise ModelManagerError("模型路径不在本地模型目录内，已拒绝删除")
        # 模型库删除只移出注册表，保留模型文件，避免误删用户训练成果。
        # 之后仍可通过导入/重新登记恢复到模型库。
        save_installed_models(self.project_dir, [item for item in models if item.id != model_id])

    def rename_model(self, model_id: str, new_name: str, active_model_id: str | None = None) -> dict[str, Any]:
        """安全修改模型显示名称和可读目录名，内部 ID 永远保持不变。"""

        validate_model_id(model_id)
        if active_model_id == model_id:
            raise ModelManagerError("当前正在使用该角色，请先切换角色后再重命名")
        name = unicodedata.normalize("NFC", str(new_name or "").strip())
        if not name:
            raise ModelManagerError("模型名称不能为空")
        if len(name) > 80:
            raise ModelManagerError("模型名称最多 80 个字符")
        if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
            raise ModelManagerError("模型名称不能包含 \\/ : * ? \" < > | 等特殊字符")
        if name.endswith((".", " ")):
            raise ModelManagerError("模型名称不能以空格或句号结尾")

        models = load_installed_models(self.project_dir)
        current = next((item for item in models if item.id == model_id), None)
        if current is None:
            raise ModelManagerError(f"模型未安装：{model_id}")
        if any(item.id != model_id and item.name.casefold() == name.casefold() for item in models):
            raise ModelManagerError(f"已存在同名模型：{name}")

        root = self._model_root(current)
        character_root = self.character_root.resolve()
        if character_root not in root.parents or not root.is_dir():
            raise ModelManagerError("模型目录不存在或不在本地模型目录内")
        metadata_path = root / "model.json"
        try:
            original_metadata_text = metadata_path.read_text(encoding="utf-8-sig")
            metadata = json.loads(original_metadata_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelManagerError(f"模型配置文件无法读取：{metadata_path}") from exc
        if not isinstance(metadata, dict) or str(metadata.get("id", model_id)) != model_id:
            raise ModelManagerError("模型配置中的内部 ID 无效")

        safe_prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        safe_prefix = safe_prefix[:48] or "model"
        target = character_root / f"{safe_prefix}__{model_id}"
        if target == root:
            target = root
        elif target.exists():
            raise ModelManagerError("目标模型目录已存在，请换一个名称")

        metadata["name"] = name
        metadata["display_name"] = name
        renamed = InstalledModel(
            id=current.id,
            name=name,
            version=current.version,
            path=str(Path("models") / target.name).replace("\\", "/"),
            files=current.files,
            sha256=current.sha256,
            min_app_version=current.min_app_version,
            min_engine_version=current.min_engine_version,
        )
        moved = False
        try:
            if target != root:
                root.rename(target)
                moved = True
            metadata_path = target / "model.json"
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            save_installed_models(
                self.project_dir,
                [renamed if item.id == model_id else item for item in models],
            )
        except (OSError, ModelCatalogError) as exc:
            try:
                if target != root and target.exists():
                    metadata_path.write_text(original_metadata_text, encoding="utf-8")
                    target.rename(root)
            except OSError:
                pass
            raise ModelManagerError(f"重命名模型失败，已尝试恢复原目录：{exc}") from exc
        return {**renamed.to_dict(), **metadata, "installed": True, "source": "local"}


def _ow_model_root(self, model: InstalledModel) -> Path:
    path = Path(model.path)
    if path.is_absolute():
        return path.resolve()
    # 新路径为 models/<id>；同时兼容旧版本的 models/characters/<id> 注册表。
    if path.parts[:1] == ("models",):
        path = Path(path.name)
    return (self.character_root / path).resolve()


def _ow_write_default_avatar(path: Path, label: object) -> None:
    """为没有自定义头像的本地模型生成轻量 SVG 占位头像。"""

    text = html.escape(str(label or "?")[:2])
    path.write_text(
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"256\" height=\"256\" viewBox=\"0 0 256 256\">"
        "<rect width=\"256\" height=\"256\" rx=\"48\" fill=\"#CC785C\"/>"
        f"<text x=\"128\" y=\"150\" text-anchor=\"middle\" font-family=\"Arial\" font-size=\"96\" fill=\"#11111b\">{text}</text>"
        "</svg>",
        encoding="utf-8",
    )


def _ow_migrate_legacy_model_dirs(self) -> None:
    """把旧 data/models/characters/<id> 安全迁移到 data/models/<id>。"""

    legacy_root = self.character_root / "characters"
    if legacy_root == self.character_root or not legacy_root.is_dir():
        return
    self.character_root.mkdir(parents=True, exist_ok=True)
    for child in list(legacy_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            validate_model_id(child.name)
        except ModelCatalogError:
            continue
        target = self.character_root / child.name
        if target.exists():
            continue
        try:
            child.rename(target)
        except OSError:
            continue
    try:
        models = load_installed_models(self.project_dir)
    except ModelCatalogError:
        return
    changed = False
    normalized = []
    for model in models:
        path = model.path.replace("\\", "/")
        if path.startswith("models/characters/"):
            value = model.to_dict()
            value["path"] = f"models/{model.id}"
            model = InstalledModel.from_dict(value)
            changed = True
        normalized.append(model)
    if changed:
        save_installed_models(self.project_dir, normalized)

ModelManager._model_root = _ow_model_root
ModelManager._migrate_legacy_model_dirs = _ow_migrate_legacy_model_dirs

# 兼容 GPT-SoVITS 训练输出：目录没有 model.json 时自动生成本地元数据。

_ow_structured_import = ModelManager.import_model

def _ow_import_training_output(self, source_dir: str | Path) -> dict[str, Any]:
    source = Path(source_dir).expanduser().resolve()
    if (source / "model.json").is_file():
        return _ow_structured_import(self, source)
    gpt_files = sorted(source.rglob("*.ckpt")) if source.is_dir() else []
    sovits_files = sorted(source.rglob("*.pth")) if source.is_dir() else []
    wav_files = sorted(source.rglob("*.wav")) if source.is_dir() else []
    if not gpt_files or not sovits_files or not wav_files:
        return _ow_structured_import(self, source)
    model_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", source.name).strip("-._") or "local-model"
    validate_model_id(model_id[:64])
    temp_root = Path(tempfile.mkdtemp(prefix="owvoice-import-"))
    package = temp_root / model_id
    try:
        shutil.copytree(source, package)
        def relative_name(path: Path) -> str:
            return str(path.relative_to(source)).replace("\\", "/")
        metadata = {
            "id": model_id[:64],
            "name": source.name,
            "version": "0.0.0",
            "gpt_model": relative_name(gpt_files[0]),
            "sovits_model": relative_name(sovits_files[0]),
            "reference_audio": relative_name(wav_files[0]),
            "prompt_text": "",
            "prompt_language": "zh",
            "license_note": "用户本地导入；请自行确认授权。",
        }
        (package / "model.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return _ow_structured_import(self, package)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

ModelManager.import_model = _ow_import_training_output
