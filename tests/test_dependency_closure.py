from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_dependency_closure import verify


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dependency_contract_passes_project_lock() -> None:
    result = verify(PROJECT_ROOT)
    assert result["ok"] is True, result
    assert result["checked"] >= 10


def test_jieba_missing_from_pyproject_is_rejected(tmp_path: Path) -> None:
    for filename in ("dependency-contract.json", "pyproject.toml", "uv.lock"):
        (tmp_path / filename).write_text(
            (PROJECT_ROOT / filename).read_text(encoding="utf-8"), encoding="utf-8"
        )
    project = tmp_path / "pyproject.toml"
    project.write_text(project.read_text(encoding="utf-8").replace('    "jieba==0.42.1",\n', ""), encoding="utf-8")
    result = verify(tmp_path)
    assert result["ok"] is False
    assert any("pyproject.toml 缺少 jieba==0.42.1" in item for item in result["failures"])


def test_jieba_missing_from_lock_is_rejected(tmp_path: Path) -> None:
    for filename in ("dependency-contract.json", "pyproject.toml", "uv.lock"):
        (tmp_path / filename).write_text(
            (PROJECT_ROOT / filename).read_text(encoding="utf-8"), encoding="utf-8"
        )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace('name = "jieba"\nversion = "0.42.1"\n', ""),
        encoding="utf-8",
    )
    result = verify(tmp_path)
    assert result["ok"] is False
    assert any("uv.lock 缺少 jieba==0.42.1" in item for item in result["failures"])
