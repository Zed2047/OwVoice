"""OwVoice 程序 GitHub Release 更新检查。"""

from __future__ import annotations

import re
from typing import Any

import requests


class UpdateManagerError(RuntimeError):
    """更新清单获取或解析失败。"""


class UpdateManager:
    """只负责检查更新，不在后端进程内覆盖正在运行的文件。"""

    VERSION_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")

    def __init__(self, current_version: str = "0.1.2", repository: str = "Zed2047/OwVoice"):
        self.current_version = current_version.lstrip("v")
        self.release_api = f"https://api.github.com/repos/{repository}/releases/latest"

    @classmethod
    def version_key(cls, value: str) -> tuple[int, int, int]:
        match = cls.VERSION_PATTERN.match(str(value).strip())
        if not match:
            return (0, 0, 0)
        return tuple(int(part or 0) for part in match.groups())

    @staticmethod
    def _asset(assets: list[dict[str, Any]], predicate) -> dict[str, Any] | None:
        return next((item for item in assets if predicate(str(item.get("name", "")))), None)

    def check(self) -> dict[str, Any]:
        try:
            response = requests.get(self.release_api, headers={"User-Agent": "OwVoice"}, timeout=(8, 20))
            response.raise_for_status()
            release = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UpdateManagerError("无法连接 GitHub 检查更新，请稍后重试。") from exc

        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise UpdateManagerError("GitHub 最新 Release 信息无效。")
        tag = str(release.get("tag_name", "")).strip()
        latest_version = tag.lstrip("v")
        assets = [item for item in release.get("assets", []) if isinstance(item, dict)]
        app_asset = self._asset(
            assets,
            lambda name: name.startswith("OwVoice-") and name.endswith(".zip"),
        )
        manifest_asset = self._asset(assets, lambda name: name == f"release-manifest-{tag}.json")
        result: dict[str, Any] = {
            "currentVersion": self.current_version,
            "latestVersion": latest_version,
            "app": {"available": self.version_key(latest_version) > self.version_key(self.current_version)},
            "releaseUrl": str(release.get("html_url", "")),
        }
        if app_asset:
            result["app"].update(
                {
                    "downloadUrl": app_asset.get("browser_download_url"),
                    "size": app_asset.get("size", 0),
                    "sha256": None,
                }
            )
        if manifest_asset and manifest_asset.get("browser_download_url"):
            try:
                manifest_response = requests.get(
                    manifest_asset["browser_download_url"],
                    headers={"User-Agent": "OwVoice"},
                    timeout=(8, 20),
                )
                manifest_response.raise_for_status()
                manifest = manifest_response.json()
                if isinstance(manifest, dict):
                    result["app"]["sha256"] = str(manifest.get("sha256", "")).lower() or None
            except (requests.RequestException, ValueError):
                result["app"]["sha256"] = None

        return result
