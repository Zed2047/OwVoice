from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.atomic_json as atomic_json


def test_atomic_json_replaces_valid_file_without_leaving_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "状态.json"
    path.write_text('{"value":"old"}\n', encoding="utf-8")

    atomic_json.write_json_atomic(path, {"value": "新值"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": "新值"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_keeps_old_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "job.json"
    original = b'{"value":"old"}\n'
    path.write_bytes(original)

    def fail_replace(_source: str | bytes, _destination: str | bytes) -> None:
        raise OSError("模拟替换失败")

    monkeypatch.setattr(atomic_json.os, "replace", fail_replace)

    with pytest.raises(OSError, match="模拟替换失败"):
        atomic_json.write_json_atomic(path, {"value": "new"})

    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_validates_payload_before_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "job.json"
    original = b'{"value":"old"}\n'
    path.write_bytes(original)

    with pytest.raises(TypeError):
        atomic_json.write_json_atomic(path, {"bad": object()})

    assert path.read_bytes() == original
