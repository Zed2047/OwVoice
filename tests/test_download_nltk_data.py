import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


def load_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "download_nltk_data.py"
    spec = importlib.util.spec_from_file_location("owvoice_download_nltk_data", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DownloadNltkDataTests(unittest.TestCase):
    def test_resource_lock_packages_are_fixed(self):
        module = load_module()
        self.assertEqual(set(module.PACKAGES), {
            "averaged_perceptron_tagger",
            "averaged_perceptron_tagger_eng",
            "cmudict",
        })
        self.assertTrue(all(len(item["sha256"]) == 64 for item in module.PACKAGES.values()))

    def test_safe_extract_rejects_directory_traversal(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "bad")
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(RuntimeError):
                    module.safe_extract_all(archive, root / "extract")
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
