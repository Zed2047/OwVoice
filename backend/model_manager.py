"""OwVoice 本地模型库管理。公开版不提供联网模型下载。"""

from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
import unicodedata
from functools import wraps
from pathlib import Path
from typing import Any

from backend.model_catalog import (
    InstalledModel,
    ModelCatalogError,
    get_character_root,
    get_model_catalog_lock,
    load_installed_models,
    register_installed_model,
    save_installed_models,
    validate_model_id,
)
from backend.atomic_json import write_json_atomic
from backend.text_encoding import read_text_compat


class ModelManagerError(RuntimeError):
    """模型操作失败。"""


def _catalog_transaction(method):
    """让同一项目的模型文件与注册表变更处于同一临界区。"""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._catalog_lock:
            return method(self, *args, **kwargs)

    return wrapped


class ModelManager:
    """只管理用户本地导入或本地训练得到的模型。"""

    FORBIDDEN_EXTENSIONS = {".exe", ".dll", ".py", ".pyc", ".ps1", ".bat", ".cmd", ".com", ".scr", ".sh"}


    def __init__(self, project_dir: str | Path, index_url: str | None = None) -> None:
        del index_url
        self.project_dir = Path(project_dir).resolve()
        self.character_root = get_character_root(self.project_dir)
        self._catalog_lock = get_model_catalog_lock(self.project_dir)
        with self._catalog_lock:
            self._migrate_legacy_model_dirs()

    def _model_root(self, model: InstalledModel) -> Path:
        path = Path(model.path)
        return (path if path.is_absolute() else self.character_root / path).resolve()

    def _validate_metadata(self, metadata: object, model_id: str, model_root: Path) -> dict[str, Any]:
        if not isinstance(metadata, dict) or str(metadata.get("id", model_id)) != model_id:
            raise ModelManagerError("model.json 的 id 无效")
        root = model_root.resolve()
        extensions = {
            "gpt_model": {".ckpt"},
            "sovits_model": {".pth"},
            "reference_audio": {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma"},
        }
        for field in ("gpt_model", "sovits_model", "reference_audio"):
            value = str(metadata.get(field, "")).strip()
            if not value or Path(value).is_absolute() or ":" in value:
                raise ModelManagerError(f"model.json 缺少有效的 {field}")
            resolved = (root / value).resolve()
            if root not in resolved.parents or not resolved.is_file():
                raise ModelManagerError(f"模型文件不存在或路径非法：{field}")
            if resolved.suffix.casefold() not in extensions[field]:
                allowed = "、".join(sorted(extensions[field]))
                raise ModelManagerError(f"model.json 的 {field} 文件类型无效，仅支持 {allowed}")
        prompt_language = str(metadata.get("prompt_language", "zh")).strip().lower()
        if prompt_language not in {"zh", "yue", "en", "ja", "ko"}:
            raise ModelManagerError("model.json 的 prompt_language 无效")
        inference = metadata.get("inference", {})
        if inference is not None and not isinstance(inference, dict):
            raise ModelManagerError("model.json 的 inference 必须是对象")
        if isinstance(inference, dict):
            ranges = {
                "top_k": (int, 1, 100),
                "top_p": (float, 0.0, 1.0),
                "temperature": (float, 0.0, 2.0),
                "sample_steps": (int, 4, 128),
            }
            for field, (converter, minimum, maximum) in ranges.items():
                if field not in inference:
                    continue
                try:
                    converted = converter(inference[field])
                except (TypeError, ValueError) as exc:
                    raise ModelManagerError(f"model.json 的 inference.{field} 类型无效") from exc
                lower_valid = converted > minimum if field in {"top_p", "temperature"} else converted >= minimum
                if not lower_valid or converted > maximum:
                    raise ModelManagerError(f"model.json 的 inference.{field} 超出允许范围")
            if "cut_punc" in inference and (
                not isinstance(inference["cut_punc"], str) or len(inference["cut_punc"]) > 64
            ):
                raise ModelManagerError("model.json 的 inference.cut_punc 无效")
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

    def _model_record(self, metadata: dict[str, Any], model_root: Path) -> InstalledModel:
        """根据已校验的模型目录生成注册项，目录名与模型 ID 可以不同。"""

        model_id = str(metadata["id"])
        files = [
            str(item.relative_to(model_root)).replace("\\", "/")
            for item in model_root.rglob("*")
            if item.is_file()
        ]
        return InstalledModel(
            id=model_id,
            name=str(metadata.get("display_name") or metadata.get("name") or model_id),
            version=str(metadata.get("version", "0.0.0")),
            path=str(Path("models") / model_root.name).replace("\\", "/"),
            files=sorted(files),
        )

    def _finish_in_place_import(
        self,
        source: Path,
        metadata: dict[str, Any],
        metadata_changed: bool,
    ) -> dict[str, Any]:
        """登记已经位于 data/models 一级目录中的模型，不复制大文件。"""

        metadata_path = source / "model.json"
        avatar_path = source / "avatar.svg"
        original_metadata: bytes | None = None
        metadata_existed = metadata_path.exists()
        avatar_existed = avatar_path.exists()
        try:
            if metadata_changed:
                if metadata_existed:
                    original_metadata = metadata_path.read_bytes()
                if metadata.get("avatar") == "avatar.svg" and not avatar_existed:
                    _ow_write_default_avatar(avatar_path, metadata.get("name", metadata["id"]))
                write_json_atomic(metadata_path, metadata)
            model = self._model_record(metadata, source)
            register_installed_model(self.project_dir, model)
            return {**model.to_dict(), **metadata, "installed": True, "source": "local"}
        except (OSError, ModelCatalogError) as exc:
            rollback_errors: list[str] = []
            if original_metadata is not None:
                try:
                    metadata_path.write_bytes(original_metadata)
                except OSError as rollback_exc:
                    rollback_errors.append(f"model.json 恢复失败：{rollback_exc}")
            elif not metadata_existed and metadata_path.exists():
                try:
                    metadata_path.unlink()
                except OSError as rollback_exc:
                    rollback_errors.append(f"临时 model.json 清理失败：{rollback_exc}")
            if not avatar_existed and avatar_path.exists():
                try:
                    avatar_path.unlink()
                except OSError as rollback_exc:
                    rollback_errors.append(f"临时头像清理失败：{rollback_exc}")
            if rollback_errors:
                raise ModelManagerError(
                    f"无法登记模型目录：{source}（{exc}）；且回滚不完整：{'；'.join(rollback_errors)}"
                ) from exc
            raise ModelManagerError(f"无法登记模型目录：{source}（{exc}）") from exc

    @_catalog_transaction
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
                metadata = json.loads(read_text_compat(root / "model.json"))
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

    @_catalog_transaction
    def import_model(self, source_dir: str | Path) -> dict[str, Any]:
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise ModelManagerError(f"模型目录不存在：{source}")
        if not (source / "model.json").is_file():
            if source == self.character_root:
                raise ModelManagerError("不能把模型文件直接放在 data\\models 根目录，请选择单个模型目录或模型集合目录")
            raise ModelManagerError("请选择包含 model.json 的单个模型目录")
        try:
            metadata = json.loads(read_text_compat(source / "model.json"))
            if not isinstance(metadata, dict):
                raise ModelManagerError("model.json 顶层必须是对象")
            model_id = str(metadata.get("id", "")).strip()
            validate_model_id(model_id)
            metadata["id"] = model_id
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
        except (OSError, UnicodeError, json.JSONDecodeError, ModelCatalogError) as exc:
            raise ModelManagerError(f"模型元数据无效：{exc}") from exc

        return self._commit_model_import(source, metadata, metadata_changed)

    def _commit_model_import(
        self,
        source: Path,
        metadata: dict[str, Any],
        metadata_changed: bool,
    ) -> dict[str, Any]:
        """提交已校验模型；调用方必须持有模型目录事务锁。"""

        model_id = str(metadata["id"])
        try:
            registered_models = load_installed_models(self.project_dir)
        except ModelCatalogError as exc:
            raise ModelManagerError(f"模型注册表无效：{exc}") from exc
        registered = next((item for item in registered_models if item.id == model_id), None)
        same_directory = next(
            (item for item in registered_models if self._model_root(item) == source),
            None,
        )
        if same_directory is not None and same_directory.id != model_id:
            raise ModelManagerError(
                f"该目录已登记为模型 {same_directory.id}，但当前 model.json 的 id 是 {model_id}。"
                "请恢复正确的 model.json，或先从模型库移除旧登记。"
            )
        if registered is not None:
            registered_root = self._model_root(registered)
            if source == registered_root:
                result = {**registered.to_dict(), **metadata, "installed": True, "source": "local"}
                result["already_installed"] = True
                return result
            if registered_root.exists():
                raise ModelManagerError(
                    f"模型 ID 已存在：{model_id}（当前目录：{registered_root}）"
                )
            # 注册项仍在但目录已丢失时，允许重新导入；最终登记会原子替换旧条目。

        try:
            self.character_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelManagerError(f"无法创建本地模型库目录：{self.character_root}") from exc
        character_root = self.character_root.resolve()
        if source.parent == character_root:
            return self._finish_in_place_import(source, metadata, metadata_changed)

        target = character_root / model_id
        if target.exists():
            raise ModelManagerError(
                f"目标目录已存在但未登记：{target}。"
                "请在导入窗口选择当前 data\\models 文件夹进行原地登记，"
                "或修改外部模型 model.json 中的 id 后再导入。"
            )
        staging_parent = Path(tempfile.mkdtemp(prefix="owvoice-model-staging-", dir=self.character_root))
        staging = staging_parent / model_id
        installed_target = False
        try:
            shutil.copytree(source, staging)
            if metadata_changed:
                if metadata.get("avatar") == "avatar.svg" and not (staging / "avatar.svg").exists():
                    _ow_write_default_avatar(staging / "avatar.svg", metadata.get("name", model_id))
                write_json_atomic(staging / "model.json", metadata)
            staging.rename(target)
            installed_target = True
            model = self._model_record(metadata, target)
            register_installed_model(self.project_dir, model)
            return {**model.to_dict(), **metadata, "installed": True, "source": "local"}
        except (OSError, ModelCatalogError) as exc:
            rollback_error = None
            if installed_target and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError as cleanup_exc:
                    rollback_error = cleanup_exc
            if rollback_error is not None:
                raise ModelManagerError(
                    f"导入模型失败：{exc}；且临时目标目录清理失败：{target}（{rollback_error}）"
                ) from exc
            raise ModelManagerError(f"导入模型失败：{exc}") from exc
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    @_catalog_transaction
    def _import_generated_package(
        self,
        source: Path,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """为训练输出补元数据后直接提交，避免大权重经过两次完整复制。"""

        try:
            self._validate_package_files(source)
            self._validate_metadata(metadata, str(metadata["id"]), source)
        except (OSError, UnicodeError, ModelCatalogError) as exc:
            raise ModelManagerError(f"模型元数据无效：{exc}") from exc
        return self._commit_model_import(source, metadata, metadata_changed=True)

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
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for source in sources:
            try:
                result = self.import_model(source)
                if result.pop("already_installed", False):
                    skipped.append({"source": str(source), "id": str(result.get("id", ""))})
                else:
                    imported.append(result)
            except ModelManagerError as exc:
                failed.append({"source": str(source), "error": str(exc)})
        return {"imported": imported, "skipped": skipped, "failed": failed}

    def install_model(self, model_id: str) -> dict[str, Any]:
        del model_id
        raise ModelManagerError("OwVoice 已停止联网模型下载，请先导入本地模型")

    def update_model(self, model_id: str, active_model_id: str | None = None) -> dict[str, Any]:
        del model_id, active_model_id
        raise ModelManagerError("OwVoice 不提供模型联网更新，请导入或重新训练本地模型")

    @_catalog_transaction
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

    @_catalog_transaction
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
            original_metadata = metadata_path.read_bytes()
            metadata = json.loads(read_text_compat(metadata_path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
            write_json_atomic(metadata_path, metadata)
            save_installed_models(
                self.project_dir,
                [renamed if item.id == model_id else item for item in models],
            )
        except (OSError, ModelCatalogError) as exc:
            rollback_errors: list[str] = []
            rollback_metadata_path = target / "model.json" if target.exists() else root / "model.json"
            try:
                rollback_metadata_path.write_bytes(original_metadata)
            except OSError as rollback_exc:
                rollback_errors.append(f"model.json 恢复失败：{rollback_exc}")
            if moved and target.exists():
                try:
                    target.rename(root)
                except OSError as rollback_exc:
                    rollback_errors.append(f"目录恢复失败：{rollback_exc}")
            if rollback_errors:
                raise ModelManagerError(
                    f"重命名模型失败：{exc}；且回滚不完整：{'；'.join(rollback_errors)}"
                ) from exc
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

def _ow_import_training_output(
    self,
    source_dir: str | Path,
    selected_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = Path(source_dir).expanduser().resolve()
    if (source / "model.json").is_file():
        return _ow_structured_import(self, source)
    gpt_files = sorted(source.rglob("*.ckpt")) if source.is_dir() else []
    sovits_files = sorted(source.rglob("*.pth")) if source.is_dir() else []
    wav_files = sorted(source.rglob("*.wav")) if source.is_dir() else []
    if not gpt_files or not sovits_files or not wav_files:
        return _ow_structured_import(self, source)
    selected_files = selected_files or {}

    def select(key: str, label: str, candidates: list[Path]) -> Path:
        if len(candidates) == 1:
            return candidates[0]
        selected_value = str(selected_files.get(key, "")).strip()
        selected = Path(selected_value).expanduser().resolve() if selected_value else None
        if selected is None or selected not in [item.resolve() for item in candidates]:
            raise ModelManagerError(f"训练目录包含多个 {label} 候选文件，请明确选择后再导入。")
        return selected

    selected_gpt = select("gpt", "GPT", gpt_files)
    selected_sovits = select("sovits", "SoVITS", sovits_files)
    selected_wav = select("reference", "参考音频", wav_files)
    model_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", source.name).strip("-._") or "local-model"
    validate_model_id(model_id[:64])
    def relative_name(path: Path) -> str:
        return str(path.relative_to(source)).replace("\\", "/")

    metadata = {
        "id": model_id[:64],
        "name": source.name,
        "version": "0.0.0",
        "gpt_model": relative_name(selected_gpt),
        "sovits_model": relative_name(selected_sovits),
        "reference_audio": relative_name(selected_wav),
        "prompt_text": "",
        "prompt_language": "zh",
        "license_note": "用户本地导入；请自行确认授权。",
    }
    return self._import_generated_package(source, metadata)

ModelManager.import_model = _ow_import_training_output
