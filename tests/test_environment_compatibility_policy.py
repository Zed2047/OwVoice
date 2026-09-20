from __future__ import annotations

import json
from pathlib import Path

from backend.update_manager import UpdateManager
from scripts.environment.verify_environment import python_version_compatible


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_environment_spec_supports_all_cpython_310_patches() -> None:
    spec = json.loads((PROJECT_ROOT / "environment-spec.json").read_text(encoding="utf-8"))
    python = spec["python"]

    assert python["version"] == "3.10.10"
    assert python["version_range"] == ">=3.10,<3.11"
    assert python["compatible_major_minor"] == "3.10"
    assert python["tested_versions"] == ["3.10.10", "3.10.21"]


def test_python_compatibility_accepts_31021_but_rejects_other_minor_versions() -> None:
    spec = {
        "implementation": "CPython",
        "version": "3.10.10",
        "version_range": ">=3.10,<3.11",
        "compatible_major_minor": "3.10",
        "architecture": "x64",
    }

    assert python_version_compatible(
        {"implementation": "CPython", "version": "3.10.10", "architecture": "x64"}, spec
    )
    assert python_version_compatible(
        {"implementation": "CPython", "version": "3.10.21", "architecture": "x64"}, spec
    )
    assert not python_version_compatible(
        {"implementation": "CPython", "version": "3.11.0", "architecture": "x64"}, spec
    )


def test_environment_core_clones_compatible_venv_before_uv_sync() -> None:
    source = (
        PROJECT_ROOT / "scripts" / "environment" / "OwVoice.Environment.psm1"
    ).read_text(encoding="utf-8-sig")

    clone = source.index('Invoke-OwVoiceTrackedProcess -FilePath "robocopy.exe"')
    sync = source.index("Invoke-OwVoiceTrackedProcess -FilePath $UvPath -Arguments $arguments")
    assert clone < sync
    assert "Test-OwVoicePythonCompatible" in source
    assert '$source = "cloned"' in source


def test_update_manager_treats_31021_as_compatible() -> None:
    target = {
        "python_version": "3.10.10",
        "python_version_range": ">=3.10,<3.11",
        "python_compatible_major_minor": "3.10",
        "architecture": "x64",
        "pyproject_sha256": "a" * 64,
        "lock_sha256": "b" * 64,
        "resource_schema": 2,
    }
    local = {
        "identityKnown": True,
        "state": {
            "python_version": "3.10.21",
            "architecture": "x64",
            "pyproject_sha256": "a" * 64,
            "lock_sha256": "b" * 64,
            "resource_schema": 2,
        },
    }

    assert UpdateManager._environment_needs_migration(target, local) is False


def test_legacy_bridge_does_not_force_utf8_console_code_page() -> None:
    batch = (PROJECT_ROOT / "修复更新器.bat").read_text(encoding="ascii")
    bridge = (PROJECT_ROOT / "scripts" / "repair_update_legacy.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "chcp 65001" not in batch.lower()
    assert "[Console]::OutputEncoding" not in bridge
