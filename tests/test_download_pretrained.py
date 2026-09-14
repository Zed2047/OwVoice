import importlib.util
import tempfile
import unittest
import zipfile
import os
from pathlib import Path
from unittest.mock import patch


def load_download_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "download_pretrained.py"
    spec = importlib.util.spec_from_file_location("owvoice_download_pretrained", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DownloadPretrainedTests(unittest.TestCase):
    def test_ready_does_not_require_optional_full_language_model(self):
        module = load_download_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            required = (root / "one.bin", root / "nested" / "two.bin")
            for path in required:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"ok")
            with patch.object(module, "REQUIRED_FILES", required), patch.object(module, "TRAINING_REQUIRED_FILES", ()):
                self.assertTrue(module.ready())

    def test_file_matches_checks_size_and_sha256(self):
        module = load_download_module()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            target.write_bytes(b"OwVoice")
            self.assertTrue(module.file_matches(target, expected_size=7, expected_sha256="2d12fcf8e4d27d51ce1ac75a7a5000abd16079d2f9e72c66c8a8a8c3a0d2663a"))
            self.assertFalse(module.file_matches(target, expected_size=8))

    def test_language_model_download_failure_allows_lite_fallback(self):
        module = load_download_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(module, "TARGET_DIR", root), patch.object(module, "download_file", side_effect=RuntimeError("network unavailable")):
                self.assertFalse(module.ensure_fasttext_lid())
            self.assertFalse((root / "fast_langdetect" / "lid.176.bin").exists())

    def test_safe_extract_rejects_directory_traversal(self):
        module = load_download_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "bad")
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(RuntimeError):
                    module.safe_extract_all(archive, root / "extract")
            self.assertFalse((root / "outside.txt").exists())

    def test_resource_lock_contains_fixed_external_sources(self):
        module = load_download_module()
        self.assertTrue(module.FFMPEG_URL.endswith("ffmpeg-9.0.1-essentials_build.zip"))
        self.assertEqual(len(module.FFMPEG_ARCHIVE_SHA256), 64)
        self.assertEqual(module.G2PW_ARCHIVE_SIZE, 588857174)
        self.assertEqual(len(module.REQUIRED_FILE_CHECKS), 12)

    def test_unpinned_fasttext_override_is_ignored_by_default(self):
        module = load_download_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def fake_download(url, target, **kwargs):
                calls.append(url)
                raise RuntimeError("offline")

            with patch.object(module, "TARGET_DIR", root), patch.object(module, "download_file", side_effect=fake_download), patch.dict(
                os.environ, {"OWVOICE_LID_URL": "https://invalid.example/lid.176.bin"}, clear=False
            ):
                self.assertFalse(module.ensure_fasttext_lid())
            self.assertEqual(calls, [module.FASTTEXT_LID_URL])


if __name__ == "__main__":
    unittest.main()
