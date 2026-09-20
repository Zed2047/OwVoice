import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from backend.release_manifest import ManifestError, build_manifest, validate_manifest


def valid_manifest() -> dict:
    return {
        "schema": 3,
        "version": "v0.2.0",
        "channel": "stable",
        "published_at": "2026-09-15T00:00:00Z",
        "minimum_updater_version": 4,
        "package": {
            "name": "OwVoice-v0.2.0.zip",
            "size_bytes": 42,
            "sha256": "a" * 64,
        },
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


class ReleaseManifestTests(unittest.TestCase):
    def test_valid_schema_three_manifest_is_accepted(self):
        self.assertEqual(validate_manifest(valid_manifest(), expected_tag="v0.2.0")["schema"], 3)

    def test_manifest_rejects_missing_environment_identity(self):
        manifest = valid_manifest()
        del manifest["environment"]["lock_sha256"]
        with self.assertRaises(ManifestError):
            validate_manifest(manifest, expected_tag="v0.2.0")

    def test_manifest_rejects_unknown_schema_and_extra_fields(self):
        unknown_schema = valid_manifest()
        unknown_schema["schema"] = 4
        with self.assertRaises(ManifestError):
            validate_manifest(unknown_schema, expected_tag="v0.2.0")

        extra = valid_manifest()
        extra["unexpected"] = True
        with self.assertRaises(ManifestError):
            validate_manifest(extra, expected_tag="v0.2.0")

    def test_manifest_rejects_package_identity_or_hash_mismatch(self):
        manifest = valid_manifest()
        manifest["package"]["name"] = "other.zip"
        with self.assertRaises(ManifestError):
            validate_manifest(manifest, expected_tag="v0.2.0")

        manifest = valid_manifest()
        manifest["package"]["sha256"] = "bad"
        with self.assertRaises(ManifestError):
            validate_manifest(manifest, expected_tag="v0.2.0")

    def test_build_manifest_calculates_package_identity_and_reads_python_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "OwVoice-v0.2.0.zip"
            archive.write_bytes(b"archive")
            (root / "environment-spec.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "python": {
                            "implementation": "CPython",
                            "version": "3.10.10",
                            "version_range": ">=3.10,<3.11",
                            "compatible_major_minor": "3.10",
                            "tested_versions": ["3.10.10", "3.10.21"],
                            "architecture": "x64",
                        },
                        "dependencies": {"pyproject_sha256": "b" * 64, "lock_sha256": "c" * 64},
                        "resources": {"schema": 2, "lock_file": "resource-lock.json"},
                        "estimated_download_bytes": {"cpu": 1, "gpu": 2, "training_extra": 3},
                        "minimum_temporary_space_bytes": {"cpu": 4, "gpu": 5},
                    }
                ),
                encoding="utf-8",
            )
            manifest = build_manifest("v0.2.0", archive, root, published_at="2026-09-15T00:00:00Z")
            self.assertEqual(manifest["package"]["size_bytes"], 7)
            self.assertEqual(manifest["package"]["sha256"], hashlib.sha256(b"archive").hexdigest())
            self.assertEqual(manifest["environment"]["python_version"], "3.10.10")
            self.assertEqual(manifest["environment"]["python_version_range"], ">=3.10,<3.11")
            self.assertEqual(manifest["environment"]["resource_schema"], 2)


if __name__ == "__main__":
    unittest.main()
