"""OwVoice 更新检查、下载和提示界面。"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QSettings, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


def format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_megabytes(size: int) -> str:
    """下载过程统一以 MB 展示，避免长字节数影响可读性。"""

    return f"{max(0, size) / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    download_url: str
    sha256: str
    size: int
    release_url: str
    notes: str
    environment: dict[str, Any] | None = None
    requires_environment_migration: bool = False
    environment_mode: str | None = None
    environment_with_training: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "UpdateInfo":
        app = payload.get("app") or {}
        try:
            size = int(app.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(
            current_version=str(payload.get("currentVersion", "")).strip(),
            latest_version=str(payload.get("latestVersion", "")).strip(),
            download_url=str(app.get("downloadUrl") or "").strip(),
            sha256=str(app.get("sha256") or "").strip().lower(),
            size=size,
            release_url=str(payload.get("releaseUrl") or "").strip(),
            notes=str(payload.get("releaseNotes") or "").strip(),
            environment=app.get("environment") if isinstance(app.get("environment"), dict) else None,
            requires_environment_migration=bool(app.get("requiresEnvironmentMigration")),
            environment_mode=str(app.get("environmentMode") or "").upper() or None,
            environment_with_training=bool(app.get("environmentWithTraining")),
        )

    def environment_download_size(self, mode: str, with_training: bool) -> int | None:
        if not isinstance(self.environment, dict):
            return None
        estimates = self.environment.get("estimated_download_bytes")
        if not isinstance(estimates, dict):
            return None
        value = estimates.get(mode.lower())
        training = estimates.get("training_extra", 0) if with_training else 0
        if not isinstance(value, int) or value <= 0 or not isinstance(training, int) or training < 0:
            return None
        return value + training

    def environment_temporary_size(self, mode: str) -> int | None:
        if not isinstance(self.environment, dict):
            return None
        values = self.environment.get("minimum_temporary_space_bytes")
        value = values.get(mode.lower()) if isinstance(values, dict) else None
        return value if isinstance(value, int) and value > 0 else None

    def is_available(self, payload: dict[str, Any]) -> bool:
        return bool((payload.get("app") or {}).get("available"))

    def is_installable(self) -> bool:
        download = urlparse(self.download_url)
        return (
            download.scheme == "https"
            and bool(download.netloc)
            and bool(re.fullmatch(r"[0-9a-f]{64}", self.sha256))
            and self.size > 0
            and bool(re.fullmatch(r"[0-9A-Za-z.+-]+", self.latest_version))
        )


class UpdatePreferences:
    """只记录用户明确跳过的版本；手动检查不受它影响。"""

    def __init__(self, settings: QSettings | None = None) -> None:
        self.settings = settings or QSettings("OwVoice", "OwVoice")

    def skipped_version(self) -> str:
        return str(self.settings.value("updates/skippedVersion", "") or "")

    def is_skipped(self, version: str) -> bool:
        return bool(version) and self.skipped_version() == version

    def skip(self, version: str) -> None:
        self.settings.setValue("updates/skippedVersion", version)
        self.settings.sync()

    def clear(self) -> None:
        self.settings.remove("updates/skippedVersion")
        self.settings.sync()


class UpdateCheckWorker(QObject):
    loaded = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.api_url = api_url.rstrip("/")

    @Slot()
    def run(self) -> None:
        try:
            response = requests.get(f"{self.api_url}/api/updates", timeout=(5, 40))
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and detail.strip():
                    raise RuntimeError(detail.strip())
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("更新服务返回的数据格式无效。")
            self.loaded.emit(payload)
        except requests.Timeout:
            self.failed.emit("连接更新服务超时。当前版本未改变，请稍后重试。")
        except requests.ConnectionError:
            self.failed.emit("暂时无法连接更新服务。当前版本未改变，请检查网络后重试。")
        except Exception as exc:  # noqa: BLE001 - 交给主界面显示可恢复错误
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class UpdateDownloadWorker(QObject):
    progress = Signal(int)
    progress_detail = Signal(str)
    completed = Signal(str)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, info: UpdateInfo, destination: Path) -> None:
        super().__init__()
        self.info = info
        self.destination = destination

    @Slot()
    def run(self) -> None:
        temporary = self.destination.with_suffix(self.destination.suffix + ".part")
        try:
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            received = 0
            with requests.get(self.info.download_url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        thread = QThread.currentThread()
                        if thread is not None and thread.isInterruptionRequested():
                            raise InterruptedError
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        percent = min(99, int(received * 100 / self.info.size))
                        self.progress.emit(percent)
                        self.progress_detail.emit(
                            f"已下载 {format_megabytes(received)} / "
                            f"{format_megabytes(self.info.size)}（{percent}%）"
                        )
                    output.flush()
                    os.fsync(output.fileno())
            if received != self.info.size:
                raise RuntimeError(
                    "更新包大小不一致：应为 "
                    f"{format_megabytes(self.info.size)}，实际为 {format_megabytes(received)}。"
                )
            if digest.hexdigest().lower() != self.info.sha256:
                raise RuntimeError("更新包校验失败，文件可能不完整，请重新下载。")
            os.replace(temporary, self.destination)
            self.progress.emit(100)
            self.completed.emit(str(self.destination))
        except InterruptedError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 下载失败可在弹窗中重试
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


def verified_archive(path: Path, info: UpdateInfo) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != info.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower() == info.sha256
    except OSError:
        return False


class UpdateDialog(QDialog):
    """新版本提示和下载状态机；只有校验通过后才允许安装。"""

    def __init__(
        self,
        info: UpdateInfo,
        archive_path: Path,
        parent=None,
        *,
        skip_checked: bool = False,
    ) -> None:
        super().__init__(parent)
        self.info = info
        self.archive_path = archive_path
        self.result_action = "later"
        self.download_thread: QThread | None = None
        self.download_worker: UpdateDownloadWorker | None = None
        self._archive_ready = False
        self._close_after_cancel = False
        self._skip_checked = skip_checked
        self._migration_confirmed = False
        self.setObjectName("UpdateDialog")
        self.setWindowTitle("OwVoice 更新")
        self.setModal(True)
        self.setMinimumWidth(570)
        self.setMaximumWidth(680)
        self.setMinimumHeight(590)
        self.resize(620, 640)
        self._build_ui()
        if verified_archive(self.archive_path, self.info):
            self._archive_ready = True
            self._set_ready()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        kicker = QLabel("OWVOICE / UPDATE")
        kicker.setObjectName("UpdateKicker")
        title = QLabel(f"发现新版本 {self.info.latest_version}")
        title.setObjectName("UpdateTitle")
        meta = QLabel(
            f"当前版本 {self.info.current_version or '未知'}  ·  更新包 {format_file_size(self.info.size)}"
        )
        meta.setObjectName("UpdateMeta")
        layout.addWidget(kicker)
        layout.addWidget(title)
        layout.addWidget(meta)

        notes_title = QLabel("本次更新")
        notes_title.setObjectName("UpdateSectionTitle")
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("UpdateNotes")
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(165)
        self.notes.setPlainText(self.info.notes or "请查看发布页面了解完整更新内容。")
        layout.addWidget(notes_title)
        layout.addWidget(self.notes)

        preserve = QLabel("更新会保留本地模型、头像、输出和配置。")
        preserve.setObjectName("UpdatePreserve")
        preserve.setWordWrap(True)
        layout.addWidget(preserve)

        self.environment_notice = QLabel()
        self.environment_notice.setObjectName("UpdateEnvironmentNotice")
        self.environment_notice.setWordWrap(True)
        self.environment_notice.setVisible(self.info.requires_environment_migration)
        layout.addWidget(self.environment_notice)
        self.environment_mode_picker = QComboBox()
        self.environment_mode_picker.setObjectName("UpdateEnvironmentMode")
        self.environment_mode_picker.addItems(["CPU", "GPU"])
        if self.info.environment_mode in {"CPU", "GPU"}:
            self.environment_mode_picker.setCurrentText(self.info.environment_mode)
        self.environment_training_checkbox = QCheckBox("同时保留本地训练组件")
        self.environment_training_checkbox.setObjectName("UpdateEnvironmentTraining")
        self.environment_training_checkbox.setChecked(self.info.environment_with_training)
        self.environment_consent_checkbox = QCheckBox("我理解并同意在健康检查通过后切换到新的项目环境")
        self.environment_consent_checkbox.setObjectName("UpdateEnvironmentConsent")
        for widget in (self.environment_mode_picker, self.environment_training_checkbox, self.environment_consent_checkbox):
            widget.setVisible(self.info.requires_environment_migration)
            layout.addWidget(widget)
        self.environment_mode_picker.currentTextChanged.connect(self._update_environment_notice)
        self.environment_training_checkbox.toggled.connect(self._update_environment_notice)
        self.environment_consent_checkbox.toggled.connect(self._migration_consent_changed)
        self._update_environment_notice()

        self.status_label = QLabel("可以先下载并校验；准备安装时 OwVoice 才会关闭。")
        self.status_label.setObjectName("UpdateStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("UpdateProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.skip_checkbox = QCheckBox(f"不再提醒此版本（{self.info.latest_version}）")
        self.skip_checkbox.setObjectName("UpdateSkip")
        self.skip_checkbox.setChecked(self._skip_checked)
        layout.addWidget(self.skip_checkbox)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.notes_button = QPushButton("查看更新说明")
        self.notes_button.setObjectName("UpdateLinkButton")
        release_url = urlparse(self.info.release_url)
        self.notes_button.setEnabled(release_url.scheme == "https" and bool(release_url.netloc))
        self.notes_button.clicked.connect(self._open_release_notes)
        self.later_button = QPushButton("稍后提醒")
        self.later_button.setObjectName("UpdateSecondaryButton")
        self.later_button.clicked.connect(self.reject)
        self.primary_button = QPushButton("下载更新")
        self.primary_button.setObjectName("UpdatePrimaryButton")
        self.primary_button.clicked.connect(self._primary_action)
        buttons.addWidget(self.notes_button)
        buttons.addStretch(1)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.primary_button)
        layout.addLayout(buttons)

        self.setStyleSheet(
            """
            QDialog#UpdateDialog { background: #F5F4ED; color: #141413; font-family: "Microsoft YaHei UI", "Microsoft YaHei"; }
            QLabel { background: transparent; }
            QLabel#UpdateKicker { color: #CC785C; font-family: Consolas; font-size: 11px; font-weight: 700; }
            QLabel#UpdateTitle { color: #141413; font-size: 27px; font-weight: 700; }
            QLabel#UpdateMeta { color: #5E5D59; font-size: 13px; }
            QLabel#UpdateSectionTitle { color: #141413; font-size: 14px; font-weight: 700; margin-top: 4px; }
            QPlainTextEdit#UpdateNotes { background: #FAF9F5; color: #4D4C48; border: 1px solid #E8E6DC; border-radius: 9px; padding: 10px; font-size: 13px; }
            QLabel#UpdatePreserve { background: #E9F3EA; color: #3E7449; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
            QLabel#UpdateEnvironmentNotice { background: #FFF0D9; color: #80571C; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
            QLabel#UpdateStatus { color: #5E5D59; font-size: 13px; }
            QCheckBox#UpdateSkip { color: #5E5D59; spacing: 8px; font-size: 13px; }
            QProgressBar#UpdateProgress { background: #E8E6DC; border: none; border-radius: 5px; height: 10px; text-align: center; color: #4D4C48; }
            QProgressBar#UpdateProgress::chunk { background: #C96442; border-radius: 5px; }
            QPushButton { border-radius: 8px; padding: 9px 15px; font-size: 14px; font-weight: 600; }
            QPushButton#UpdatePrimaryButton { background: #C96442; color: #FAF9F5; border: none; }
            QPushButton#UpdatePrimaryButton:hover { background: #B95738; }
            QPushButton#UpdatePrimaryButton:pressed { background: #A84A30; }
            QPushButton#UpdateSecondaryButton, QPushButton#UpdateLinkButton { background: transparent; color: #4D4C48; border: 1px solid #D1CFC5; }
            QPushButton#UpdateSecondaryButton:hover, QPushButton#UpdateLinkButton:hover { background: #E8E6DC; color: #141413; }
            QPushButton:disabled { background: rgba(20, 20, 19, 0.10); color: rgba(20, 20, 19, 0.38); border-color: transparent; }
            """
        )

    def _open_release_notes(self) -> None:
        if self.info.release_url:
            QDesktopServices.openUrl(QUrl(self.info.release_url))

    def _primary_action(self) -> None:
        if self._archive_ready:
            if self.info.requires_environment_migration and not self.environment_consent_checkbox.isChecked():
                self.status_label.setText("请先确认环境迁移；取消不会修改当前程序或环境。")
                self.status_label.setStyleSheet("color: #C64545;")
                return
            self.result_action = "install"
            self.accept()
            return
        self._start_download()

    def _start_download(self) -> None:
        if self.download_thread is not None:
            return
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.status_label.setText("正在下载更新，当前版本仍可继续使用……")
        self.status_label.setStyleSheet("color: #5E5D59;")
        self.primary_button.setText("正在下载…")
        self.primary_button.setEnabled(False)
        self.later_button.setText("取消下载")
        self.later_button.setEnabled(True)
        self.skip_checkbox.setEnabled(False)
        self._ensure_content_visible()
        self.download_thread = QThread(self)
        self.download_worker = UpdateDownloadWorker(self.info, self.archive_path)
        self.download_worker.moveToThread(self.download_thread)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_worker.progress.connect(self.progress.setValue)
        self.download_worker.progress_detail.connect(self._download_progress_detail)
        self.download_worker.completed.connect(self._download_completed)
        self.download_worker.cancelled.connect(self._download_cancelled)
        self.download_worker.failed.connect(self._download_failed)
        self.download_worker.finished.connect(self.download_thread.quit)
        self.download_worker.finished.connect(self.download_worker.deleteLater)
        self.download_thread.finished.connect(self._download_finished)
        self.download_thread.start()

    @Slot(str)
    def _download_completed(self, _path: str) -> None:
        self._archive_ready = True
        self._set_ready()

    @Slot(str)
    def _download_progress_detail(self, message: str) -> None:
        self.status_label.setText(message + "\n下载期间当前版本仍可继续使用。")

    @Slot()
    def _download_cancelled(self) -> None:
        self.status_label.setText("下载已取消，未修改现有程序。")

    @Slot(str)
    def _download_failed(self, message: str) -> None:
        self.status_label.setText(f"下载失败：{message}\n请检查网络后重试。")
        self.status_label.setStyleSheet("color: #C64545;")
        self.primary_button.setText("重新下载")
        self.primary_button.setEnabled(True)
        self.later_button.setText("稍后提醒")
        self.later_button.setEnabled(True)
        self.skip_checkbox.setEnabled(True)
        self._ensure_content_visible()

    @Slot()
    def _download_finished(self) -> None:
        thread = self.download_thread
        self.download_thread = None
        self.download_worker = None
        if thread is not None:
            thread.deleteLater()
        if self._close_after_cancel:
            QDialog.reject(self)

    def _set_ready(self) -> None:
        self.progress.setVisible(True)
        self.progress.setValue(100)
        self.status_label.setText("下载和校验已完成。确认后 OwVoice 将关闭、安装并自动重新启动。")
        self.status_label.setStyleSheet("color: #4C9A5D; font-weight: 600;")
        self.primary_button.setText("确认并重启安装" if self.info.requires_environment_migration else "重启并安装")
        self.primary_button.setEnabled(not self.info.requires_environment_migration or self.environment_consent_checkbox.isChecked())
        self.later_button.setText("稍后提醒")
        self.later_button.setEnabled(True)
        self.skip_checkbox.setEnabled(True)
        self._ensure_content_visible()

    def _ensure_content_visible(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        self.resize(self.width(), max(self.height(), layout.sizeHint().height()))

    def environment_mode(self) -> str:
        return self.environment_mode_picker.currentText() if self.info.requires_environment_migration else ""

    def environment_with_training(self) -> bool:
        return self.info.requires_environment_migration and self.environment_training_checkbox.isChecked()

    def _update_environment_notice(self) -> None:
        if not self.info.requires_environment_migration:
            return
        mode = self.environment_mode_picker.currentText()
        training = self.environment_training_checkbox.isChecked()
        download = self.info.environment_download_size(mode, training)
        temporary = self.info.environment_temporary_size(mode)
        download_text = format_file_size(download) if download is not None else "发布清单未提供"
        temporary_text = format_file_size(temporary) if temporary is not None else "发布清单未提供"
        suffix = "，包含训练组件" if training else ""
        self.environment_notice.setText(
            f"本次更新需要同步 {mode} 运行环境{suffix}。兼容的 Python 3.10.x 和相同版本 Torch 会复用，"
            f"仅补齐变化项；最坏情况下载：{download_text}；临时可用空间：{temporary_text}。"
            "模型、配置、训练记录和输出不会删除。"
        )
        self._ensure_content_visible()

    def _migration_consent_changed(self, _checked: bool) -> None:
        if self._archive_ready:
            self.primary_button.setEnabled(self.environment_consent_checkbox.isChecked())

    def reject(self) -> None:
        if self.download_thread is not None:
            self._close_after_cancel = True
            self.status_label.setText("正在取消下载……")
            self.primary_button.setEnabled(False)
            self.later_button.setEnabled(False)
            self.download_thread.requestInterruption()
            return
        self.result_action = "later"
        super().reject()
