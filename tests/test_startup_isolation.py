from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from frontend.app import EngineStartWorker
from frontend.startup import StartupError, StartupWorker, ensure_backend_belongs_to


def _response(project_dir: str):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "ok",
        "owvoice": True,
        "project_dir": project_dir,
    }
    return response


def test_backend_reuse_requires_same_project_dir(tmp_path: Path):
    with patch(
        "frontend.startup.requests.get",
        return_value=_response(str(tmp_path)),
    ):
        ensure_backend_belongs_to(tmp_path)


def test_backend_from_another_install_is_rejected(tmp_path: Path):
    other_project = tmp_path / "other"
    with patch(
        "frontend.startup.requests.get",
        return_value=_response(str(other_project)),
    ):
        with pytest.raises(StartupError, match="另一套 OwVoice"):
            ensure_backend_belongs_to(tmp_path)


def test_empty_model_startup_rejects_foreign_gpt_sovits_port(tmp_path: Path):
    worker = StartupWorker(tmp_path)

    with patch("frontend.startup.port_open", return_value=True):
        with pytest.raises(StartupError, match="端口 9880"):
            worker.start_gpt_sovits(None)


def test_dynamic_engine_does_not_reuse_existing_gpt_sovits(tmp_path: Path):
    worker = EngineStartWorker(tmp_path, {})
    failures: list[str] = []
    worker.failed.connect(failures.append)

    with patch.object(worker, "_online", return_value=True):
        worker.run()

    assert failures and "端口 9880" in failures[0]
