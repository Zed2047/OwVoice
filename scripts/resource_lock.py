"""读取并严格校验 OwVoice 的统一资源锁。"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
ALLOWED_FEATURES = {"inference", "training"}
REQUIRED_IDS = {
    "ffmpeg-9.0.1-essentials", "g2pw-1.1", "fasttext-lid-176", "gpt-sovits-pretrained",
    "nltk-averaged-perceptron-tagger", "nltk-averaged-perceptron-tagger-eng", "nltk-cmudict",
}


def _validate_relative_path(value: Any) -> None:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if not str(value) or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise RuntimeError(f"资源锁路径无效：{value}")


def _validate_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise RuntimeError(f"资源锁 {label} SHA256 无效")


def validate_resource_lock(lock: dict[str, Any]) -> dict[str, Any]:
    if lock.get("schema") != 2 or not isinstance(lock.get("resources"), list):
        raise RuntimeError("不支持的资源锁 schema")
    seen: set[str] = set()
    for resource in lock["resources"]:
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id or resource_id in seen:
            raise RuntimeError(f"资源锁 ID 无效或重复：{resource_id}")
        seen.add(resource_id)
        required_for = resource.get("required_for")
        if not isinstance(required_for, list) or not set(required_for).issubset(ALLOWED_FEATURES):
            raise RuntimeError(f"资源锁 required_for 无效：{resource_id}")
        sources = resource.get("sources")
        if not isinstance(sources, list) or not sources:
            raise RuntimeError(f"资源锁缺少来源：{resource_id}")
        for source in sources:
            if not isinstance(source, dict) or source.get("kind") not in {"primary", "mirror"}:
                raise RuntimeError(f"资源锁来源类型无效：{resource_id}")
            if not str(source.get("url", "")).startswith("https://"):
                raise RuntimeError(f"资源锁只允许 HTTPS 来源：{resource_id}")
        if resource.get("archive") is None and resource.get("artifact") is None:
            raise RuntimeError(f"资源锁缺少归档或快照描述：{resource_id}")
        archive = resource.get("archive")
        if archive is not None:
            if not isinstance(archive.get("size_bytes"), int) or archive["size_bytes"] <= 0:
                raise RuntimeError(f"资源锁归档大小无效：{resource_id}")
            _validate_sha256(archive.get("sha256"), f"{resource_id} archive")
        installed = resource.get("installed_files")
        if not isinstance(installed, list) or not installed:
            raise RuntimeError(f"资源锁缺少已安装文件清单：{resource_id}")
        paths: set[str] = set()
        for item in installed:
            if not isinstance(item, dict) or not isinstance(item.get("size_bytes"), int) or item["size_bytes"] <= 0:
                raise RuntimeError(f"资源锁已安装文件属性无效：{resource_id}")
            item_required_for = item.get("required_for")
            if item_required_for is not None and (
                not isinstance(item_required_for, list)
                or not item_required_for
                or not set(item_required_for).issubset(ALLOWED_FEATURES)
            ):
                raise RuntimeError(f"资源锁已安装文件 required_for 无效：{resource_id}")
            _validate_relative_path(item.get("path"))
            normalized = str(item["path"]).replace("\\", "/").lower()
            if normalized in paths:
                raise RuntimeError(f"资源锁已安装文件重复：{resource_id}/{item['path']}")
            paths.add(normalized)
            _validate_sha256(item.get("sha256"), f"{resource_id}/{item['path']}")
        _validate_relative_path(resource.get("target"))
        if not resource.get("license") or not str(resource.get("source_page", "")).startswith("https://"):
            raise RuntimeError(f"资源锁许可证或来源页面缺失：{resource_id}")
    missing = REQUIRED_IDS - seen
    if missing:
        raise RuntimeError(f"资源锁缺少必需资源：{sorted(missing)}")
    return lock


def load_resource_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"资源锁文件无效：{path}") from exc
    if not isinstance(lock, dict):
        raise RuntimeError("资源锁根节点必须是对象")
    return validate_resource_lock(lock)


def index_resources(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(resource["id"]): resource for resource in lock["resources"]}


def primary_source(resource: dict[str, Any]) -> str:
    for source in resource["sources"]:
        if source["kind"] == "primary":
            return str(source["url"])
    raise RuntimeError(f"资源没有 primary 来源：{resource['id']}")


def installed_file_map(resource: dict[str, Any]) -> dict[str, tuple[int, str]]:
    return {
        str(item["path"]): (int(item["size_bytes"]), str(item["sha256"]).lower())
        for item in resource["installed_files"]
    }
