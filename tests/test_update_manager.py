import unittest
from unittest.mock import Mock, patch

from backend.update_manager import UpdateManager


def response(payload):
    result = Mock()
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


class UpdateManagerTests(unittest.TestCase):
    def test_version_key_handles_tags_and_prerelease_suffixes(self):
        self.assertEqual(UpdateManager.version_key("v0.2.0"), (0, 2, 0))
        self.assertEqual(UpdateManager.version_key("1.4"), (1, 4, 0))
        self.assertEqual(UpdateManager.version_key("v2.0.1-beta"), (2, 0, 1))

    def test_check_accepts_only_matching_v2_manifest_and_archive(self):
        release = {"tag_name": "v0.2.0", "draft": False, "prerelease": False, "html_url": "https://example.invalid/release", "assets": [
            {"name": "OwVoice-v0.2.0.zip", "browser_download_url": "https://example.invalid/app", "size": 42},
            {"name": "UpdateBridge-v0.2.0.zip", "browser_download_url": "https://example.invalid/bridge"},
            {"name": "release-manifest-v2-v0.2.0.json", "browser_download_url": "https://example.invalid/manifest"},
        ]}
        manifest = {"schema": 2, "version": "v0.2.0", "archive_name": "OwVoice-v0.2.0.zip", "sha256": "a" * 64, "size_bytes": 42}
        with patch("backend.update_manager.requests.get", side_effect=[response(release), response(manifest)]):
            result = UpdateManager("0.1.2").check()
        self.assertTrue(result["app"]["available"])
        self.assertEqual(result["app"]["downloadUrl"], "https://example.invalid/app")
        self.assertEqual(result["app"]["sha256"], "a" * 64)

    def test_check_rejects_legacy_manifest_format(self):
        release = {"tag_name": "v0.2.0", "draft": False, "prerelease": False, "assets": [
            {"name": "OwVoice-v0.2.0.zip", "browser_download_url": "https://example.invalid/app"},
            {"name": "release-manifest-v2-v0.2.0.json", "browser_download_url": "https://example.invalid/manifest"},
        ]}
        with patch("backend.update_manager.requests.get", side_effect=[response(release), response({"archive_name": "OwVoice-v0.2.0.zip", "sha256": "b" * 64})]):
            result = UpdateManager("0.1.2").check()
        self.assertIsNone(result["app"]["sha256"])


if __name__ == "__main__":
    unittest.main()
