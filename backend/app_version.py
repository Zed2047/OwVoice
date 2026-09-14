"""OwVoice 发布身份的唯一运行时读取入口。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SUPPORTED_CHANNELS = {"stable", "beta", "dev"}


class ReleaseIdentityError(RuntimeError):
    """version.json 缺失或内容无效。"""


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    channel: str
    update_schema: int

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def default_project_dir() -> Path:
    return Path(os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1])).resolve()


def load_release_identity(project_dir: str | Path | None = None) -> ReleaseIdentity:
    root = Path(project_dir).resolve() if project_dir is not None else default_project_dir()
    path = root / "version.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseIdentityError(f"无法读取发布身份文件：{path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseIdentityError("version.json 顶层必须是对象")

    version = str(payload.get("version", "")).strip()
    channel = str(payload.get("channel", "")).strip().lower()
    update_schema = payload.get("updateSchema")
    if not SEMVER_PATTERN.fullmatch(version):
        raise ReleaseIdentityError(f"version.json 的 version 无效：{version!r}")
    if channel not in SUPPORTED_CHANNELS:
        raise ReleaseIdentityError(f"version.json 的 channel 无效：{channel!r}")
    if isinstance(update_schema, bool) or not isinstance(update_schema, int) or update_schema < 1:
        raise ReleaseIdentityError("version.json 的 updateSchema 必须是正整数")
    return ReleaseIdentity(version=version, channel=channel, update_schema=update_schema)


def get_app_version(
    project_dir: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """返回应用版本；测试覆盖必须同时显式开启，避免遗留环境变量污染正式版本。"""

    env = os.environ if environment is None else environment
    override = str(env.get("OWVOICE_APP_VERSION", "")).strip()
    allow_override = str(env.get("OWVOICE_ALLOW_VERSION_OVERRIDE", "")).strip().lower()
    if override and allow_override in {"1", "true", "yes", "on"}:
        if not SEMVER_PATTERN.fullmatch(override):
            raise ReleaseIdentityError(f"OWVOICE_APP_VERSION 无效：{override!r}")
        return override
    return load_release_identity(project_dir).version
