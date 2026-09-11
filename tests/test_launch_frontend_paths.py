import os
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.launch_frontend import resolve_project_dir


def test_source_mode_allows_explicit_project_override(tmp_path: Path):
    with patch.object(sys, "frozen", False, create=True), patch.dict(
        os.environ,
        {"OWVOICE_PROJECT_DIR": str(tmp_path)},
    ):
        assert resolve_project_dir() == tmp_path.resolve()


def test_frozen_mode_ignores_stale_project_environment(tmp_path: Path):
    install_dir = tmp_path / "新安装"
    (install_dir / "config").mkdir(parents=True)
    (install_dir / "data" / "models").mkdir(parents=True)
    fake_exe = install_dir / "OwVoice.exe"
    with patch.object(sys, "frozen", True, create=True), patch.object(
        sys,
        "executable",
        str(fake_exe),
    ), patch.dict(
        os.environ,
        {"OWVOICE_PROJECT_DIR": r"D:\旧安装\OwVoice"},
    ):
        assert resolve_project_dir() == install_dir.resolve()
