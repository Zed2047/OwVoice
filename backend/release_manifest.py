"""Release manifest schema 3 的生成和严格校验。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """发布清单不符合协议。"""


_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
_TOP_LEVEL_KEYS = {
    "schema",
    "version",
    "channel",
    "published_at",
    "minimum_updater_version",
    "package",
    "environment",
}
_TOP_LEVEL_OPTIONAL_KEYS = {"release_notes"}
_PACKAGE_KEYS = {"name", "size_bytes", "sha256"}
_ENVIRONMENT_KEYS = {
    "schema",
    "python_version",
    "python_version_range",
    "python_compatible_major_minor",
    "python_tested_versions",
    "architecture",
    "pyproject_sha256",
    "lock_sha256",
    "resource_schema",
    "migration_required",
    "estimated_download_bytes",
    "minimum_temporary_space_bytes",
}


def _require_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ManifestError(f"{label} 包含未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ManifestError(f"{label} 缺少字段：{', '.join(sorted(missing))}")


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise ManifestError(f"{label} 必须是 64 位十六进制 SHA256")
    return value.lower()


def _require_nonnegative_int(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        comparator = "正整数" if positive else "非负整数"
        raise ManifestError(f"{label} 必须是{comparator}")
    return value


def _validate_bytes_map(value: Any, label: str, keys: set[str]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManifestError(f"{label} 字段不完整")
    return {key: _require_nonnegative_int(value[key], f"{label}.{key}", positive=True) for key in keys}


def validate_manifest(manifest: dict[str, Any], *, expected_tag: str | None = None) -> dict[str, Any]:
    """校验并返回规范化的 schema 3 manifest。"""

    if not isinstance(manifest, dict):
        raise ManifestError("manifest 根节点必须是对象")
    unknown = set(manifest) - (_TOP_LEVEL_KEYS | _TOP_LEVEL_OPTIONAL_KEYS)
    missing = _TOP_LEVEL_KEYS - set(manifest)
    if unknown:
        raise ManifestError(f"manifest 包含未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ManifestError(f"manifest 缺少字段：{', '.join(sorted(missing))}")
    if manifest["schema"] != 3:
        raise ManifestError("manifest schema 必须为 3")
    tag = manifest["version"]
    if not isinstance(tag, str) or not _VERSION_PATTERN.fullmatch(tag):
        raise ManifestError("manifest version 必须是 vX.Y.Z")
    if expected_tag is not None and tag != expected_tag:
        raise ManifestError(f"manifest version 与目标版本不一致：{tag} / {expected_tag}")
    if manifest["channel"] not in {"stable", "beta", "dev"}:
        raise ManifestError("manifest channel 无效")
    if not isinstance(manifest["published_at"], str):
        raise ManifestError("manifest published_at 无效")
    if "release_notes" in manifest and not isinstance(manifest["release_notes"], str):
        raise ManifestError("manifest release_notes 无效")
    try:
        datetime.fromisoformat(manifest["published_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("manifest published_at 不是有效 ISO 时间") from exc
    _require_nonnegative_int(manifest["minimum_updater_version"], "minimum_updater_version", positive=True)

    package = manifest["package"]
    if not isinstance(package, dict):
        raise ManifestError("manifest package 必须是对象")
    _require_keys(package, _PACKAGE_KEYS, "package")
    expected_name = f"OwVoice-{tag}.zip"
    if package["name"] != expected_name:
        raise ManifestError(f"package.name 必须为 {expected_name}")
    _require_nonnegative_int(package["size_bytes"], "package.size_bytes", positive=True)
    _require_hash(package["sha256"], "package.sha256")

    environment = manifest["environment"]
    if not isinstance(environment, dict):
        raise ManifestError("manifest environment 必须是对象")
    _require_keys(environment, _ENVIRONMENT_KEYS, "environment")
    if environment["schema"] != 1:
        raise ManifestError("environment schema 必须为 1")
    if not isinstance(environment["python_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", environment["python_version"]):
        raise ManifestError("environment.python_version 无效")
    if environment["python_version_range"] != ">=3.10,<3.11":
        raise ManifestError("environment.python_version_range 无效")
    if environment["python_compatible_major_minor"] != "3.10":
        raise ManifestError("environment.python_compatible_major_minor 无效")
    if environment["python_tested_versions"] != ["3.10.10", "3.10.21"]:
        raise ManifestError("environment.python_tested_versions 无效")
    if environment["architecture"] != "x64":
        raise ManifestError("environment.architecture 必须为 x64")
    _require_hash(environment["pyproject_sha256"], "environment.pyproject_sha256")
    _require_hash(environment["lock_sha256"], "environment.lock_sha256")
    _require_nonnegative_int(environment["resource_schema"], "environment.resource_schema", positive=True)
    if not isinstance(environment["migration_required"], bool):
        raise ManifestError("environment.migration_required 必须是布尔值")
    _validate_bytes_map(environment["estimated_download_bytes"], "environment.estimated_download_bytes", {"cpu", "gpu", "training_extra"})
    _validate_bytes_map(environment["minimum_temporary_space_bytes"], "environment.minimum_temporary_space_bytes", {"cpu", "gpu"})

    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_environment_identity(project_root: Path) -> dict[str, Any]:
    """从发布包内的 environment-spec.json 生成目标环境身份。"""

    spec_path = Path(project_root) / "environment-spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取环境规范：{spec_path}") from exc
    try:
        python = spec["python"]
        dependencies = spec["dependencies"]
        resources = spec["resources"]
    except KeyError as exc:
        raise ManifestError(f"环境规范缺少字段：{exc.args[0]}") from exc
    estimates = spec.get("estimated_download_bytes")
    temporary = spec.get("minimum_temporary_space_bytes")
    _validate_bytes_map(estimates, "environment-spec.estimated_download_bytes", {"cpu", "gpu", "training_extra"})
    _validate_bytes_map(temporary, "environment-spec.minimum_temporary_space_bytes", {"cpu", "gpu"})
    return {
        "schema": 1,
        "python_version": str(python["version"]),
        "python_version_range": str(python["version_range"]),
        "python_compatible_major_minor": str(python["compatible_major_minor"]),
        "python_tested_versions": [str(value) for value in python["tested_versions"]],
        "architecture": str(python["architecture"]),
        "pyproject_sha256": str(dependencies["pyproject_sha256"]).lower(),
        "lock_sha256": str(dependencies["lock_sha256"]).lower(),
        "resource_schema": int(resources["schema"]),
        "migration_required": True,
        "estimated_download_bytes": {key: int(estimates.get(key, 0)) for key in ("cpu", "gpu", "training_extra")},
        "minimum_temporary_space_bytes": {key: int(temporary.get(key, 0)) for key in ("cpu", "gpu")},
    }


def build_manifest(
    version: str,
    archive_path: Path,
    project_root: Path,
    *,
    published_at: str,
    channel: str = "stable",
    minimum_updater_version: int = 4,
    release_notes: str | None = None,
) -> dict[str, Any]:
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise ManifestError(f"发布包不存在：{archive_path}")
    manifest = {
        "schema": 3,
        "version": version,
        "channel": channel,
        "published_at": published_at,
        "minimum_updater_version": minimum_updater_version,
        "package": {
            "name": archive_path.name,
            "size_bytes": archive_path.stat().st_size,
            "sha256": _sha256(archive_path),
        },
        "environment": build_environment_identity(Path(project_root)),
    }
    if release_notes is not None:
        manifest["release_notes"] = release_notes
    return validate_manifest(manifest, expected_tag=version)
