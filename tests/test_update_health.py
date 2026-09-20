from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.update_health import inspect_update_health


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_environment(
    root: Path, *, mode: str = "CPU", with_training: bool = False
) -> str:
    pyproject = "project"
    lock = "lock"
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "uv.lock").write_text(lock, encoding="utf-8")
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
                "dependencies": {
                    "pyproject_sha256": _sha256_text(pyproject),
                    "lock_sha256": _sha256_text(lock),
                },
                "resources": {"schema": 2, "lock_file": "resource-lock.json"},
            }
        ),
        encoding="utf-8",
    )
    fingerprint_input = (
        f">=3.10,<3.11|{_sha256_text(pyproject)}|{_sha256_text(lock)}|{mode}|"
        f"{str(with_training).lower()}"
    )
    fingerprint = _sha256_text(fingerprint_input)
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "environment-state.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "project_root": str(root.resolve()),
                "mode": mode,
                "with_training": with_training,
                "environment_fingerprint": fingerprint,
                "python_version": "3.10.10",
                "python_version_range": ">=3.10,<3.11",
                "architecture": "x64",
                "pyproject_sha256": _sha256_text(pyproject),
                "lock_sha256": _sha256_text(lock),
                "resource_schema": 2,
                "verified": True,
            }
        ),
        encoding="utf-8",
    )
    return fingerprint


def _write_project(root: Path) -> str:
    fingerprint = _write_environment(root)
    (root / "config").mkdir()
    (root / "config" / "voices.local.json").write_text(
        '{"version":1,"voices":[]}', encoding="utf-8"
    )
    model_root = root / "data" / "models" / "voice-1"
    model_root.mkdir(parents=True)
    (model_root / "model.json").write_text(
        '{"id":"voice-1","name":"Voice"}', encoding="utf-8"
    )
    (root / "data" / "models" / "installed-models.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "models": [
                    {
                        "id": "voice-1",
                        "name": "Voice",
                        "version": "1.0.0",
                        "path": "models/voice-1",
                        "files": ["model.json"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    resource = root / "resources" / "required.bin"
    resource.parent.mkdir()
    resource.write_bytes(b"required")
    (root / "resource-lock.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "resources": [
                    {
                        "id": "required",
                        "required_for": ["inference"],
                        "target": "resources",
                        "installed_files": [
                            {"path": "required.bin", "size_bytes": len(b"required")}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return fingerprint


def test_update_health_accepts_readable_cpu_install(tmp_path: Path) -> None:
    fingerprint = _write_project(tmp_path)

    result = inspect_update_health(tmp_path)

    assert result["ok"] is True
    assert result["environment"]["fingerprint"] == fingerprint
    assert result["configuration"]["ok"] is True
    assert result["model_registry"] == {"ok": True, "count": 1, "error": None}
    assert result["resources"]["ok"] is True


def test_update_health_rejects_corrupt_model_registry(tmp_path: Path) -> None:
    _write_project(tmp_path)
    (tmp_path / "data" / "models" / "installed-models.json").write_text(
        "{broken", encoding="utf-8"
    )

    result = inspect_update_health(tmp_path)

    assert result["ok"] is False
    assert result["model_registry"]["ok"] is False


def test_update_health_rejects_environment_fingerprint_mismatch(tmp_path: Path) -> None:
    _write_project(tmp_path)
    state_path = tmp_path / ".runtime" / "environment-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["environment_fingerprint"] = "0" * 64
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = inspect_update_health(tmp_path)

    assert result["ok"] is False
    assert result["environment"]["ok"] is False
    assert "environment_fingerprint" in result["environment"]["error"]


def test_update_health_rejects_missing_required_resource(tmp_path: Path) -> None:
    _write_project(tmp_path)
    (tmp_path / "resources" / "required.bin").unlink()

    result = inspect_update_health(tmp_path)

    assert result["ok"] is False
    assert result["resources"]["ok"] is False


def test_update_health_filters_training_only_files_by_environment_mode(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    lock_path = tmp_path / "resource-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    resource = lock["resources"][0]
    resource["required_for"] = ["inference", "training"]
    resource["installed_files"].append(
        {
            "path": "training-only.bin",
            "size_bytes": 8,
            "required_for": ["training"],
        }
    )
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    inference_result = inspect_update_health(tmp_path)
    assert inference_result["resources"]["ok"] is True

    _write_environment(tmp_path, with_training=True)
    training_result = inspect_update_health(tmp_path)
    assert training_result["resources"]["ok"] is False
    assert "training-only.bin" in training_result["resources"]["error"]
