"""更新提交前使用的只读健康检查。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import struct
import sys
from pathlib import Path
from typing import Any

from backend.model_catalog import MODEL_ID_PATTERN
from backend.text_encoding import read_text_compat


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(read_text_compat(path))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 顶层必须是对象")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _python_version_compatible(version: str, python_spec: dict[str, Any]) -> bool:
    return ".".join(str(version).split(".")[:2]) == str(
        python_spec.get("compatible_major_minor", "")
    )


def _environment_health(root: Path) -> dict[str, Any]:
    try:
        state = _read_json_object(root / ".runtime" / "environment-state.json")
        spec = _read_json_object(root / "environment-spec.json")
        dependencies = spec["dependencies"]
        python_spec = spec["python"]
        resources = spec["resources"]
        if state.get("schema") != 1 or spec.get("schema") != 1:
            raise ValueError("环境状态或规范 schema 无效")
        mode = str(state.get("mode", "")).upper()
        if mode not in {"CPU", "GPU"}:
            raise ValueError("环境模式无效")
        with_training = state.get("with_training") is True
        pyproject_hash = _sha256(root / "pyproject.toml")
        lock_hash = _sha256(root / "uv.lock")
        expected_input = "|".join(
            (
                str(python_spec.get("version_range", python_spec["version"])),
                str(dependencies["pyproject_sha256"]).lower(),
                str(dependencies["lock_sha256"]).lower(),
                mode,
                str(with_training).lower(),
            )
        )
        expected_fingerprint = hashlib.sha256(expected_input.encode("utf-8")).hexdigest()
        checks = {
            "project_root": str(state.get("project_root", "")).casefold()
            == str(root).casefold(),
            "verified": state.get("verified") is True,
            "environment_fingerprint": str(
                state.get("environment_fingerprint", "")
            ).lower()
            == expected_fingerprint,
            "python_version": _python_version_compatible(
                str(state.get("python_version", "")), python_spec
            ),
            "python_version_range": str(state.get("python_version_range", ""))
            == str(python_spec.get("version_range", "")),
            "architecture": str(state.get("architecture", ""))
            == str(python_spec["architecture"]),
            "pyproject_sha256": str(state.get("pyproject_sha256", "")).lower()
            == pyproject_hash,
            "lock_sha256": str(state.get("lock_sha256", "")).lower() == lock_hash,
            "spec_pyproject_sha256": pyproject_hash
            == str(dependencies["pyproject_sha256"]).lower(),
            "spec_lock_sha256": lock_hash
            == str(dependencies["lock_sha256"]).lower(),
            "resource_schema": state.get("resource_schema")
            == resources.get("schema"),
            "python_implementation": platform.python_implementation()
            == str(python_spec["implementation"]),
            "runtime_python_version": _python_version_compatible(
                ".".join(str(item) for item in sys.version_info[:3]), python_spec
            ),
            "runtime_architecture": (
                "x64" if struct.calcsize("P") * 8 == 64 else "x86"
            )
            == str(python_spec["architecture"]),
        }
        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks:
            raise ValueError(
                "环境状态、规范或当前 Python 不一致：" + ", ".join(failed_checks)
            )
        cuda_available: bool | None = None
        if mode == "GPU":
            import torch

            cuda_available = bool(torch.cuda.is_available())
            if not cuda_available:
                raise ValueError("GPU 模式下 CUDA 不可用")
        return {
            "ok": True,
            "mode": mode,
            "with_training": with_training,
            "fingerprint": expected_fingerprint,
            "cuda_available": cuda_available,
            "error": None,
        }
    except Exception as exc:  # 健康接口必须返回结构化失败，不能因损坏状态直接 500。
        return {
            "ok": False,
            "mode": None,
            "with_training": None,
            "fingerprint": None,
            "cuda_available": None,
            "error": str(exc),
        }


def _configuration_health(root: Path) -> dict[str, Any]:
    path = root / "config" / "voices.local.json"
    try:
        if path.exists():
            _read_json_object(path)
        return {"ok": True, "error": None}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _model_registry_health(root: Path) -> dict[str, Any]:
    registry_path = root / "data" / "models" / "installed-models.json"
    try:
        if not registry_path.exists():
            return {"ok": True, "count": 0, "error": None}
        registry = _read_json_object(registry_path)
        values = registry.get("models", [])
        if not isinstance(values, list):
            raise ValueError("installed-models.json 的 models 必须是数组")
        model_base = (root / "data" / "models").resolve()
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("模型注册项必须是对象")
            model_id = str(value.get("id", ""))
            if not MODEL_ID_PATTERN.fullmatch(model_id):
                raise ValueError("模型注册项 id 无效")
            relative = Path(str(value.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"模型 {model_id} 的路径无效")
            model_root = (root / "data" / relative).resolve()
            if model_root != model_base and model_base not in model_root.parents:
                raise ValueError(f"模型 {model_id} 的路径越界")
            metadata = _read_json_object(model_root / "model.json")
            if str(metadata.get("id", model_id)) != model_id:
                raise ValueError(f"模型 {model_id} 的 model.json 身份不一致")
        return {"ok": True, "count": len(values), "error": None}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "count": 0, "error": str(exc)}


def _resource_health(root: Path, *, with_training: bool) -> dict[str, Any]:
    try:
        lock = _read_json_object(root / "resource-lock.json")
        if lock.get("schema") != 2 or not isinstance(lock.get("resources"), list):
            raise ValueError("resource-lock.json schema 无效")
        checked = 0
        feature = "training" if with_training else "inference"
        for resource in lock["resources"]:
            required_for = resource.get("required_for") if isinstance(resource, dict) else None
            if not isinstance(required_for, list) or feature not in required_for:
                continue
            target = Path(str(resource.get("target", "")))
            if target.is_absolute() or ".." in target.parts:
                raise ValueError("资源目标路径无效")
            files = resource.get("installed_files")
            if not isinstance(files, list) or not files:
                raise ValueError("必需资源缺少安装文件清单")
            for item in files:
                item_required_for = item.get("required_for", required_for)
                if not isinstance(item_required_for, list) or feature not in item_required_for:
                    continue
                relative = Path(str(item.get("path", "")))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("资源文件路径无效")
                path = root / target / relative
                if not path.is_file() or path.stat().st_size != int(item.get("size_bytes", -1)):
                    raise ValueError(f"必需资源缺失或大小不符：{path}")
                checked += 1
        return {"ok": True, "checked_files": checked, "error": None}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "checked_files": 0, "error": str(exc)}


def inspect_update_health(project_dir: str | Path) -> dict[str, Any]:
    """返回可供更新事务判定是否提交的只读健康信息。"""

    root = Path(project_dir).resolve()
    environment = _environment_health(root)
    configuration = _configuration_health(root)
    model_registry = _model_registry_health(root)
    resources = _resource_health(
        root,
        with_training=environment.get("with_training") is True,
    )
    dependencies = {
        "ok": all(importlib.util.find_spec(name) is not None for name in ("fastapi", "requests"))
    }
    ok = all(
        item["ok"]
        for item in (environment, configuration, model_registry, resources, dependencies)
    )
    return {
        "ok": ok,
        "backend": {"ok": True},
        "environment": environment,
        "configuration": configuration,
        "model_registry": model_registry,
        "resources": resources,
        "dependencies": dependencies,
    }
