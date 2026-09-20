"""OwVoice 程序 GitHub Release 更新检查。"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any

import requests

from backend.app_version import get_app_version
from backend.release_manifest import ManifestError, validate_manifest


class UpdateManagerError(RuntimeError):
    """更新清单获取或解析失败。"""


class UpdateManager:
    """只负责检查更新，不在后端进程内覆盖正在运行的文件。"""

    UPDATER_VERSION = 4
    VERSION_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")

    def __init__(
        self,
        current_version: str | None = None,
        repository: str = "Zed2047/OwVoice",
        project_root: Path | None = None,
        update_feed_url: str | None = None,
    ):
        self.current_version = (current_version or get_app_version()).lstrip("v")
        self.repository = repository
        self.update_feed_url = update_feed_url or f"https://github.com/{repository}/releases/latest/download/update.json"
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1])

    def _local_environment(self) -> dict[str, Any]:
        state_path = self.project_root / ".runtime" / "environment-state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        mode = str(state.get("mode", "")).upper()
        return {
            "mode": mode if mode in {"CPU", "GPU"} else None,
            "withTraining": bool(state.get("with_training", False)),
            "identityKnown": state.get("schema") == 1,
            "state": state,
        }

    @staticmethod
    def _environment_needs_migration(target: dict[str, Any], local: dict[str, Any]) -> bool:
        state = local["state"]
        if not local["identityKnown"]:
            return True
        local_version = str(state.get("python_version", ""))
        local_major_minor = ".".join(local_version.split(".")[:2])
        if local_major_minor != str(target.get("python_compatible_major_minor", "")):
            return True
        fields = ("architecture", "pyproject_sha256", "lock_sha256", "resource_schema")
        return any(state.get(field) != target.get(field) for field in fields)

    @classmethod
    def version_key(cls, value: str) -> tuple[int, int, int]:
        match = cls.VERSION_PATTERN.match(str(value).strip())
        if not match:
            return (0, 0, 0)
        return tuple(int(part or 0) for part in match.groups())

    def check(self) -> dict[str, Any]:
        try:
            response = requests.get(self.update_feed_url, headers={"User-Agent": "OwVoice"}, timeout=(8, 20))
            response.raise_for_status()
            manifest = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UpdateManagerError("无法连接 GitHub 检查更新，请稍后重试。") from exc

        try:
            tag = str(manifest.get("version", "")).strip() if isinstance(manifest, dict) else ""
            normalized = validate_manifest(manifest, expected_tag=tag or None)
        except ManifestError as exc:
            raise UpdateManagerError(f"更新清单无效：{exc}") from exc

        latest_version = tag.lstrip("v")
        package = normalized["package"]
        archive_url = f"https://github.com/{self.repository}/releases/download/{tag}/{package['name']}"
        result: dict[str, Any] = {
            "currentVersion": self.current_version,
            "latestVersion": latest_version,
            "app": {"available": self.version_key(latest_version) > self.version_key(self.current_version)},
            "releaseUrl": f"https://github.com/{self.repository}/releases/tag/{tag}",
            "releaseNotes": str(normalized.get("release_notes", ""))[:12000],
        }
        local_environment = self._local_environment()
        result["app"].update(
            {
                "downloadUrl": archive_url,
                "size": package["size_bytes"],
                "sha256": package["sha256"],
                "manifestSchema": 3,
                "environment": normalized["environment"],
                "requiresEnvironmentMigration": self._environment_needs_migration(normalized["environment"], local_environment),
                "environmentMode": local_environment["mode"],
                "environmentWithTraining": local_environment["withTraining"],
                "updaterCompatible": normalized["minimum_updater_version"] <= self.UPDATER_VERSION,
            }
        )
        if normalized["minimum_updater_version"] > self.UPDATER_VERSION:
            result["app"]["blockedReason"] = "需要更新独立更新器后才能安装此版本。"

        return result
