from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.resource_lock import load_resource_lock, validate_resource_lock
from scripts.verify_resource_lock import verify


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_resource_lock_schema_two_is_complete() -> None:
    result = verify(PROJECT_ROOT)
    assert result["ok"] is True
    assert result["schema"] == 2
    assert result["resources"] == 7
    assert result["installed_files"] == 22
    assert result["optional_acceleration"] == ["fasttext-lid-176"]


def test_resource_lock_rejects_unpinned_http_source() -> None:
    lock = copy.deepcopy(load_resource_lock(PROJECT_ROOT / "resource-lock.json"))
    lock["resources"][0]["sources"][0]["url"] = "http://example.invalid/resource.zip"
    with pytest.raises(RuntimeError, match="HTTPS"):
        validate_resource_lock(lock)


def test_resource_lock_rejects_missing_installed_hash() -> None:
    lock = copy.deepcopy(load_resource_lock(PROJECT_ROOT / "resource-lock.json"))
    lock["resources"][0]["installed_files"][0]["sha256"] = "bad"
    with pytest.raises(RuntimeError, match="SHA256"):
        validate_resource_lock(lock)
