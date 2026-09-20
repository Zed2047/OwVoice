from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from unittest.mock import patch

import requests
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from frontend.update_ui import (
    UpdateDownloadWorker,
    UpdateCheckWorker,
    UpdateDialog,
    UpdateInfo,
    UpdatePreferences,
    format_file_size,
    format_megabytes,
    verified_archive,
)


def test_update_check_worker_uses_backend_error_detail() -> None:
    response = requests.Response()
    response.status_code = 503
    response.url = "http://127.0.0.1:8765/api/updates"
    response._content = (
        '{"detail":"原因：连接发布服务超时。\\n影响：当前版本未改变。'
        '\\n建议：稍后重试。\\n错误编号：UPDATE-201 · abc12345"}'
    ).encode("utf-8")

    failures: list[str] = []
    worker = UpdateCheckWorker("http://127.0.0.1:8765")
    worker.failed.connect(failures.append)
    with patch("frontend.update_ui.requests.get", return_value=response):
        worker.run()

    assert failures == [
        "原因：连接发布服务超时。\n影响：当前版本未改变。"
        "\n建议：稍后重试。\n错误编号：UPDATE-201 · abc12345"
    ]
    assert "503 Server Error" not in failures[0]


def test_update_failure_is_handled_on_gui_thread() -> None:
    from frontend.app import OwVoiceApp

    source = inspect.getsource(OwVoiceApp.check_updates)
    assert ".loaded.connect(self._update_check_loaded)" in source
    assert ".failed.connect(self._update_check_failed)" in source
    assert "lambda" not in source
    loaded_source = inspect.getsource(OwVoiceApp._update_check_loaded)
    assert "CREATE_NEW_CONSOLE" in loaded_source
    assert "CREATE_NO_WINDOW" not in loaded_source


def _info(content: bytes) -> UpdateInfo:
    return UpdateInfo(
        current_version="0.1.2",
        latest_version="0.2.0",
        download_url="https://example.invalid/OwVoice-v0.2.0.zip",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        release_url="https://example.invalid/release",
        notes="修复更新流程",
    )


def test_file_size_formatting() -> None:
    assert format_file_size(42) == "42 B"
    assert format_file_size(2048) == "2.0 KB"
    assert format_file_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_megabytes(5 * 1024 * 1024) == "5.0 MB"


def test_update_preferences_skip_only_selected_version(tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.IniFormat)
    preferences = UpdatePreferences(settings)
    preferences.skip("0.2.0")
    assert preferences.is_skipped("0.2.0")
    assert not preferences.is_skipped("0.2.1")
    preferences.clear()
    assert not preferences.is_skipped("0.2.0")


def test_update_info_rejects_incomplete_or_unsafe_payload() -> None:
    payload = {
        "currentVersion": "0.1.2",
        "latestVersion": "../0.2.0",
        "app": {"available": True, "downloadUrl": "https://example.invalid/app", "size": "bad"},
    }
    info = UpdateInfo.from_payload(payload)
    assert info.is_available(payload)
    assert not info.is_installable()

    insecure = _info(b"archive")
    insecure = UpdateInfo(**{**insecure.__dict__, "download_url": "http://example.invalid/app"})
    assert not insecure.is_installable()


def test_update_info_reads_environment_migration_estimates() -> None:
    payload = {
        "currentVersion": "0.1.2",
        "latestVersion": "0.2.0",
        "app": {
            "environment": {
                "estimated_download_bytes": {"cpu": 100, "gpu": 200, "training_extra": 30},
                "minimum_temporary_space_bytes": {"cpu": 300, "gpu": 400},
            },
            "requiresEnvironmentMigration": True,
            "environmentMode": "GPU",
            "environmentWithTraining": True,
        },
    }
    info = UpdateInfo.from_payload(payload)
    assert info.requires_environment_migration is True
    assert info.environment_mode == "GPU"
    assert info.environment_with_training is True
    assert info.environment_download_size("GPU", True) == 230
    assert info.environment_temporary_size("GPU") == 400


def test_environment_migration_dialog_requires_explicit_consent(tmp_path: Path) -> None:
    application = QApplication.instance() or QApplication([])
    info = UpdateInfo(
        current_version="0.1.2",
        latest_version="0.2.0",
        download_url="https://example.invalid/OwVoice-v0.2.0.zip",
        sha256="a" * 64,
        size=100,
        release_url="https://example.invalid/release",
        notes="测试",
        environment={
            "estimated_download_bytes": {"cpu": 100, "gpu": 200, "training_extra": 30},
            "minimum_temporary_space_bytes": {"cpu": 300, "gpu": 400},
        },
        requires_environment_migration=True,
        environment_mode="GPU",
        environment_with_training=True,
    )
    dialog = UpdateDialog(info, tmp_path / "archive.zip")
    dialog._archive_ready = True
    dialog._set_ready()
    assert not dialog.environment_notice.isHidden()
    assert dialog.environment_mode() == "GPU"
    assert dialog.environment_with_training() is True
    assert not dialog.primary_button.isEnabled()
    dialog.environment_consent_checkbox.setChecked(True)
    assert dialog.primary_button.isEnabled()
    dialog.close()
    assert application is not None


def test_download_worker_writes_only_verified_archive(tmp_path: Path) -> None:
    content = b"verified update archive"
    info = _info(content)
    destination = tmp_path / "OwVoice-v0.2.0.zip"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            assert chunk_size == 1024 * 1024
            yield content[:8]
            yield content[8:]

    completed: list[str] = []
    failures: list[str] = []
    progress_details: list[str] = []
    worker = UpdateDownloadWorker(info, destination)
    worker.completed.connect(completed.append)
    worker.failed.connect(failures.append)
    worker.progress_detail.connect(progress_details.append)
    with patch("frontend.update_ui.requests.get", return_value=Response()):
        worker.run()

    assert failures == []
    assert completed == [str(destination)]
    assert progress_details[-1].startswith("已下载 ")
    assert " MB / " in progress_details[-1]
    assert "字节" not in progress_details[-1]
    assert verified_archive(destination, info)
    assert not destination.with_suffix(".zip.part").exists()


def test_download_worker_removes_bad_archive(tmp_path: Path) -> None:
    expected = b"expected"
    info = _info(expected)
    destination = tmp_path / "OwVoice-v0.2.0.zip"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            yield b"bad"

    failures: list[str] = []
    worker = UpdateDownloadWorker(info, destination)
    worker.failed.connect(failures.append)
    with patch("frontend.update_ui.requests.get", return_value=Response()):
        worker.run()

    assert failures
    assert not destination.exists()
    assert not destination.with_suffix(".zip.part").exists()
