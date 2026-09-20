import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.update_manager import UpdateManager, UpdateManagerError


def response(payload):
    result = Mock()
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


def valid_manifest() -> dict:
    return {
        "schema": 3,
        "version": "v0.2.0",
        "channel": "stable",
        "published_at": "2026-09-15T00:00:00Z",
        "minimum_updater_version": 4,
        "package": {"name": "OwVoice-v0.2.0.zip", "size_bytes": 42, "sha256": "a" * 64},
        "environment": {
            "schema": 1,
            "python_version": "3.10.10",
            "python_version_range": ">=3.10,<3.11",
            "python_compatible_major_minor": "3.10",
            "python_tested_versions": ["3.10.10", "3.10.21"],
            "architecture": "x64",
            "pyproject_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "resource_schema": 2,
            "migration_required": True,
            "estimated_download_bytes": {"cpu": 1, "gpu": 2, "training_extra": 3},
            "minimum_temporary_space_bytes": {"cpu": 4, "gpu": 5},
        },
    }


class UpdateManagerTests(unittest.TestCase):
    def test_default_feed_uses_stable_release_asset(self):
        manager = UpdateManager("0.1.2")
        self.assertEqual(
            manager.update_feed_url,
            "https://github.com/Zed2047/OwVoice/releases/latest/download/update.json",
        )
        source = Path("backend/update_manager.py").read_text(encoding="utf-8")
        self.assertNotIn("api.github.com", source)

    def test_check_reads_one_stable_update_json_and_builds_direct_archive_url(self):
        feed_url = "https://example.invalid/releases/latest/download/update.json"
        with patch("backend.update_manager.requests.get", return_value=response(valid_manifest())) as get:
            result = UpdateManager("0.1.2", update_feed_url=feed_url).check()

        get.assert_called_once_with(feed_url, headers={"User-Agent": "OwVoice"}, timeout=(8, 20))
        self.assertEqual(result["app"]["manifestSchema"], 3)
        self.assertEqual(result["app"]["downloadUrl"], "https://github.com/Zed2047/OwVoice/releases/download/v0.2.0/OwVoice-v0.2.0.zip")
        self.assertEqual(result["app"]["size"], 42)
        self.assertEqual(result["app"]["sha256"], "a" * 64)
        self.assertTrue(result["app"]["requiresEnvironmentMigration"])
        self.assertEqual(result["releaseUrl"], "https://github.com/Zed2047/OwVoice/releases/tag/v0.2.0")

    def test_check_skips_environment_migration_when_local_identity_matches(self):
        root = Path(self._testMethodName)
        with patch("backend.update_manager.Path.read_text", return_value=json.dumps({
            "schema": 1, "mode": "GPU", "with_training": True,
            "python_version": "3.10.10", "architecture": "x64", "pyproject_sha256": "b" * 64,
            "lock_sha256": "c" * 64, "resource_schema": 2,
        })):
            manager = UpdateManager("0.1.2", project_root=root, update_feed_url="https://example.invalid/update.json")
            with patch("backend.update_manager.requests.get", return_value=response(valid_manifest())):
                result = manager.check()

        self.assertFalse(result["app"]["requiresEnvironmentMigration"])
        self.assertEqual(result["app"]["environmentMode"], "GPU")
        self.assertTrue(result["app"]["environmentWithTraining"])

    def test_version_key_handles_tags_and_prerelease_suffixes(self):
        self.assertEqual(UpdateManager.version_key("v0.2.0"), (0, 2, 0))
        self.assertEqual(UpdateManager.version_key("1.4"), (1, 4, 0))
        self.assertEqual(UpdateManager.version_key("v2.0.1-beta"), (2, 0, 1))

    def test_check_rejects_invalid_update_json_without_api_fallback(self):
        with patch("backend.update_manager.requests.get", return_value=response({"schema": 2})) as get:
            with self.assertRaises(UpdateManagerError):
                UpdateManager("0.1.2", update_feed_url="https://example.invalid/update.json").check()

        get.assert_called_once()

    def test_check_returns_optional_release_notes(self):
        payload = valid_manifest()
        payload["release_notes"] = "修复更新流程"
        with patch("backend.update_manager.requests.get", return_value=response(payload)):
            result = UpdateManager("0.1.2", update_feed_url="https://example.invalid/update.json").check()

        self.assertEqual(result["releaseNotes"], "修复更新流程")


if __name__ == "__main__":
    unittest.main()
