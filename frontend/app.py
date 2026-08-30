"""OwVoice 新版桌面前端。只调用 OwVoice API，不直接管理模型。"""

from __future__ import annotations

import os
import re
import sys
import ctypes
import math
import subprocess
import uuid
from pathlib import Path

import requests
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, QUrl, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QDoubleSpinBox,
    QScrollArea,
    QStackedWidget,
    QSizePolicy,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


API_URL = os.environ.get("OWVOICE_API", "http://127.0.0.1:8765").rstrip("/")
PROJECT_DIR = Path(
    os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1])
)
OUTPUT_DIR = PROJECT_DIR / "output"
ICON_PATH = PROJECT_DIR / "assets" / "OwVoice.ico"


def set_windows_app_identity() -> None:
    """Set a stable Windows taskbar identity for OwVoice."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("OwVoice.Desktop")
        )
    except (AttributeError, OSError):
        pass


class AvatarLabel(QLabel):
    """显示圆形头像；没有素材时使用带首字的占位头像。"""

    def __init__(self, name: str, avatar_path: str = "", accent: str = "#CC785C", size: int = 88):
        super().__init__()
        self.name = name
        self.avatar_path = avatar_path
        self.accent = accent
        self.is_selected = False
        self.avatar_size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_selected(self, selected: bool) -> None:
        self.is_selected = selected
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        circle = QPainterPath()
        inset = max(5, self.avatar_size // 12)
        inner_size = self.avatar_size - inset * 2
        circle.addEllipse(inset, inset, inner_size, inner_size)
        painter.setClipPath(circle)

        pixmap = QPixmap(self.avatar_path) if self.avatar_path else QPixmap()
        if not pixmap.isNull():
            side = min(pixmap.width(), pixmap.height())
            cropped = pixmap.copy(
                (pixmap.width() - side) // 2,
                (pixmap.height() - side) // 2,
                side,
                side,
            )
            painter.drawPixmap(inset, inset, inner_size, inner_size, cropped)
        else:
            painter.fillRect(inset, inset, inner_size, inner_size, QColor(self.accent))
            painter.setPen(QColor("#11111b"))
            font = QFont("Microsoft YaHei", max(18, self.avatar_size // 3))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self.name[:1] or "?")

        painter.setClipping(False)
        ring_color = QColor("#CC785C") if self.is_selected else QColor("#141413")
        ring_color.setAlpha(230 if self.is_selected else 32)
        painter.setPen(ring_color)
        painter.setBrush(Qt.NoBrush)
        ring_inset = max(3, self.avatar_size // 18)
        painter.drawEllipse(ring_inset, ring_inset, self.avatar_size - ring_inset * 2, self.avatar_size - ring_inset * 2)


class StartupWave(QWidget):
    """启动页的轻量声波标识，提供持续但克制的工作状态反馈。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0.0
        self.setFixedSize(220, 66)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self) -> None:
        self.phase = (self.phase + 0.18) % (math.tau)
        self.update()

    def stop(self) -> None:
        self.timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = self.width()
        center_y = self.height() / 2
        colors = (QColor("#C96442"), QColor("#D99A7C"), QColor("#E4C1AF"))
        for wave_index, color in enumerate(colors):
            pen = painter.pen()
            pen.setColor(color)
            pen.setWidth(2 if wave_index == 0 else 1)
            painter.setPen(pen)
            path = QPainterPath()
            amplitude = (16 - wave_index * 4) * (0.88 + 0.06 * math.sin(self.phase))
            frequency = 1.7 + wave_index * 0.18
            for x in range(width):
                ratio = x / max(1, width - 1)
                y = center_y + amplitude * math.sin(
                    ratio * math.tau * frequency + self.phase + wave_index * 0.9
                ) * (0.25 + 0.75 * math.sin(ratio * math.pi))
                if x == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.drawPath(path)


class VoiceCard(QPushButton):
    """左侧角色卡片，点击后选择对应角色。"""

    selected = Signal(str)

    def __init__(self, voice: dict, parent=None):
        super().__init__(parent)
        self.voice_id = voice["id"]
        self.setObjectName("VoiceCard")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(88)
        self.setToolTip("点击选择此角色的语音模型")

        avatar_path = str(voice.get("avatar", "")).strip()
        if avatar_path and not os.path.isabs(avatar_path):
            avatar_path = str(PROJECT_DIR / avatar_path)
        avatar = AvatarLabel(
            voice.get("display_name", voice["id"]),
            avatar_path if os.path.isfile(avatar_path) else "",
            voice.get("accent", "#CC785C"),
            size=70,
        )
        self.avatar = avatar
        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("SelectionBar")
        self.selection_bar.setFixedWidth(3)
        self.selection_bar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.selection_bar.setStyleSheet("background-color: transparent; border-radius: 1px;")
        name_label = QLabel(voice.get("display_name", voice["id"]))
        name_label.setObjectName("VoiceName")
        name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(0)
        info_layout.addWidget(name_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 12, 8)
        layout.setSpacing(10)
        layout.addWidget(self.selection_bar)
        layout.addWidget(avatar, 0, Qt.AlignVCenter)
        layout.addLayout(info_layout, 1)
        self.clicked.connect(lambda: self.selected.emit(self.voice_id))

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.avatar.set_selected(selected)
        self.selection_bar.setStyleSheet(
            "background-color: #CC785C; border-radius: 1px;"
            if selected
            else "background-color: transparent; border-radius: 1px;"
        )
        self.style().unpolish(self)
        self.style().polish(self)


class SynthesisWorker(QObject):
    success = Signal(bytes)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        api_url: str,
        voice_id: str,
        text: str,
        speed: float,
        request_id: str,
    ):
        super().__init__()
        self.api_url = api_url
        self.voice_id = voice_id
        self.text = text
        self.speed = speed
        self.request_id = request_id

    def run(self) -> None:
        try:
            response = requests.post(
                f"{self.api_url}/api/synthesize",
                json={
                    "voice_id": self.voice_id,
                    "text": self.text,
                    "speed": self.speed,
                    "request_id": self.request_id,
                },
                timeout=360,
            )
            if response.status_code != 200:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                raise RuntimeError(detail or f"请求失败：HTTP {response.status_code}")
            if len(response.content) < 44 or response.content[:4] != b"RIFF":
                raise RuntimeError("后端返回的不是完整 WAV 音频")
            self.success.emit(response.content)
        except Exception as exc:  # noqa: BLE001 - 将后端错误显示给用户
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class OwVoiceApp(QMainWindow):
    def __init__(self, startup_mode: bool = False) -> None:
        super().__init__()
        self.startup_mode = startup_mode
        self.setWindowTitle("OwVoice - 守望先锋角色语音工具")
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1040, 720)
        self.setMinimumSize(860, 620)
        self.thread: QThread | None = None
        self.worker: SynthesisWorker | None = None
        self.output_dir = OUTPUT_DIR
        self.last_audio = self.output_dir / "last.wav"
        self.last_audio_by_voice: dict[str, Path] = {}
        self.prompt_by_voice: dict[str, str] = {}
        self.pending_audio: Path | None = None
        self.pending_voice_id: str | None = None
        self.voices: list[dict] = []
        self.voice_cards: dict[str, VoiceCard] = {}
        self.current_voice_id: str | None = None
        installed_fonts = set(QFontDatabase.families())
        # 中文桌面界面统一使用一套字体，避免不同控件出现字形和基线跳变。
        self.ui_font = (
            "Microsoft YaHei UI"
            if "Microsoft YaHei UI" in installed_fonts
            else "Microsoft YaHei"
        )
        self.display_font = (
            "EB Garamond"
            if "EB Garamond" in installed_fonts
            else "Georgia"
            if "Georgia" in installed_fonts
            else self.ui_font
        )
        self.mono_font = (
            "JetBrains Mono"
            if "JetBrains Mono" in installed_fonts
            else "Consolas"
            if "Consolas" in installed_fonts
            else self.ui_font
        )

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.playback_buffer: QBuffer | None = None
        self.playback_bytes = b""
        self.play_when_loaded = False
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("输入需要合成的角色台词……")
        self.text_input.setPlainText("任务开始了。")
        self.status_label = QLabel("正在连接 OwVoice 后端……")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 300)
        self.speed_slider.setValue(100)
        self.speed_input = QDoubleSpinBox()
        self.speed_input.setRange(0.10, 3.00)
        self.speed_input.setDecimals(2)
        self.speed_input.setSingleStep(0.05)
        self.speed_input.setValue(1.00)
        self.speed_input.setSuffix("x")
        self.speed_input.setKeyboardTracking(False)
        self.speed_input.setObjectName("SpeedInput")
        self.speed_input.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.speed_input.setAlignment(Qt.AlignCenter)
        self.speed_input.setFixedWidth(108)
        self.speed_input.editingFinished.connect(self._normalize_speed_input)
        self.speed_slider.valueChanged.connect(self._sync_speed_from_slider)
        self.speed_input.valueChanged.connect(self._sync_speed_from_input)
        self.generate_button = QPushButton("合成语音")
        self.generate_button.setEnabled(False)
        self.generate_button.clicked.connect(self.generate)
        self.play_button = QPushButton("播放/暂停")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_play_pause)

        self.build_ui()
        if not self.startup_mode:
            self.load_voices()

    def build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(18)

        self.setStyleSheet(
            """
            QMainWindow, QWidget { background-color: #F5F4ED; color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; }
            QLabel { background-color: transparent; font-size: 13px; }
            QFrame#Sidebar { background-color: #E8E6DC; border: 1px solid #D8D5CA; border-radius: 14px; }
            QFrame#SectionCard, QFrame#TextCard { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 10px; }
            QFrame#BrandCard { background-color: transparent; border: none; border-bottom: 1px solid #D1CFC5; border-radius: 0px; }
            QLabel#BrandMark { color: #CC785C; font-family: '{self.mono_font}', 'Microsoft YaHei', sans-serif; font-size: 13px; font-weight: bold; }
            QLabel#BrandTitle { color: #141413; font-family: '{self.display_font}', 'Microsoft YaHei', sans-serif; font-size: 34px; font-weight: 600; }
            QLabel#BrandSubtitle { color: #5E5D59; font-size: 13px; }
            QLabel#Hint, QLabel#FieldHint { color: #5E5D59; font-size: 12px; }
            QLabel#PageKicker { color: #CC785C; font-family: '{self.mono_font}', 'Microsoft YaHei', sans-serif; font-size: 11px; font-weight: bold; }
            QLabel#PageTitle { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 32px; font-weight: 600; }
            QLabel#CurrentVoice { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 23px; font-weight: 600; }
            QLabel#VoiceName { color: #141413; background-color: transparent; font-size: 18px; font-weight: 600; }
            QLabel#SectionTitle { color: #141413; font-size: 14px; font-weight: 600; }
            QLineEdit, QTextEdit { background-color: #FFFFFF; border: 1px solid #E8E6DC; border-radius: 8px; padding: 9px 11px; color: #141413; font-size: 14px; }
            QLineEdit:focus, QTextEdit:focus { border: 1px solid #CC785C; }
            QTextEdit { padding: 13px; }
            QPushButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 8px; padding: 11px 18px; font-weight: 600; font-size: 15px; }
            QPushButton:hover { background-color: #B95738; }
            QPushButton:pressed { background-color: #A84A30; }
            QPushButton:disabled { background-color: rgba(20, 20, 19, 0.16); color: rgba(20, 20, 19, 0.42); }
            QPushButton#SecondaryButton { background-color: #E8E6DC; color: #4D4C48; border: 1px solid #D1CFC5; }
            QPushButton#SecondaryButton:hover { background-color: #DCD9CF; border-color: #BDBAB0; }
            QPushButton#VoiceCard { background-color: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0; font-size: 14px; }
            QPushButton#VoiceCard:hover { background-color: #F0EEE6; border-color: #D8D5CA; }
            QPushButton#VoiceCard:checked, QPushButton#VoiceCard[selected="true"] { background-color: #FAF9F5; border-color: #D1CFC5; }
            QSlider::groove:horizontal { border: 1px solid #D8D5CA; height: 5px; background: #E8E6DC; border-radius: 3px; }
            QSlider::handle:horizontal { background: #C96442; border: none; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
            QScrollArea { border: none; background: transparent; }
            QWidget#StartupPage { background-color: #F5F4ED; }
            QLabel#StartupKicker { color: #CC785C; font-family: '{self.mono_font}', 'Microsoft YaHei', sans-serif; font-size: 12px; font-weight: bold; }
            QLabel#StartupTitle { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 42px; font-weight: 600; }
            QLabel#StartupSubtitle { color: #5E5D59; font-size: 16px; }
            QLabel#StartupPercent { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 58px; font-weight: 600; }
            QLabel#StartupStatus { color: #141413; font-size: 16px; font-weight: 600; }
            QLabel#StartupFooter { color: #8A8881; font-size: 13px; }
            QTextEdit#StartupDetails { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 10px; padding: 8px; color: #5E5D59; }
            QProgressBar#StartupProgress { background-color: #E8E6DC; border: none; border-radius: 5px; }
            QProgressBar#StartupProgress::chunk { background-color: #C96442; border-radius: 5px; }
            QLabel#SpeedLabel { color: #141413; font-size: 15px; font-weight: 600; }
            QDoubleSpinBox#SpeedInput { background-color: #FFFFFF; border: 1px solid #D8D5CA; border-radius: 8px; padding: 4px 8px; color: #141413; font-size: 19px; font-weight: 600; min-height: 34px; }
            QDoubleSpinBox#SpeedInput:focus { border: 1px solid #CC785C; }
            """
            .replace("{self.ui_font}", self.ui_font)
            .replace("{self.display_font}", self.display_font)
            .replace("{self.mono_font}", self.mono_font)
        )

        root_layout.addWidget(self.build_sidebar())
        root_layout.addWidget(self.build_workspace(), 1)
        self.tool_page = root
        if self.startup_mode:
            self.loading_page = self.build_loading_page()
            self.page_stack = QStackedWidget()
            self.page_stack.addWidget(self.loading_page)
            self.page_stack.addWidget(self.tool_page)
            self.setCentralWidget(self.page_stack)
        else:
            self.setCentralWidget(self.tool_page)

    def build_loading_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("StartupPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(72, 52, 72, 44)
        layout.setSpacing(0)

        kicker = QLabel("OWVOICE / STARTUP")
        kicker.setObjectName("StartupKicker")
        layout.addWidget(kicker)

        layout.addStretch(1)

        hero = QVBoxLayout()
        hero.setSpacing(10)

        self.startup_wave = StartupWave()
        hero.addWidget(self.startup_wave, alignment=Qt.AlignHCenter)

        title = QLabel("正在准备语音引擎")
        title.setObjectName("StartupTitle")
        title.setAlignment(Qt.AlignCenter)
        hero.addWidget(title)

        subtitle = QLabel("首次启动可能需要一些时间，请不要关闭此窗口。")
        subtitle.setObjectName("StartupSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        hero.addWidget(subtitle)

        hero.addSpacing(14)

        self.startup_percent_label = QLabel("0%")
        self.startup_percent_label.setObjectName("StartupPercent")
        self.startup_percent_label.setAlignment(Qt.AlignCenter)
        hero.addWidget(self.startup_percent_label)

        self.startup_progress = QProgressBar()
        self.startup_progress.setObjectName("StartupProgress")
        self.startup_progress.setRange(0, 100)
        self.startup_progress.setValue(0)
        self.startup_progress.setTextVisible(False)
        self.startup_progress.setFixedHeight(10)
        self.startup_progress.setMaximumWidth(680)
        hero.addWidget(self.startup_progress, alignment=Qt.AlignHCenter)

        hero.addSpacing(6)

        self.startup_status_label = QLabel("正在启动……")
        self.startup_status_label.setObjectName("StartupStatus")
        self.startup_status_label.setWordWrap(True)
        self.startup_status_label.setAlignment(Qt.AlignCenter)
        hero.addWidget(self.startup_status_label)

        layout.addLayout(hero)
        layout.addStretch(1)

        footer = QLabel("OwVoice · 本地角色配音工作台")
        footer.setObjectName("StartupFooter")
        footer.setAlignment(Qt.AlignCenter)
        layout.addWidget(footer)

        self.startup_details = QTextEdit()
        self.startup_details.setReadOnly(True)
        self.startup_details.setObjectName("StartupDetails")
        self.startup_details.setVisible(False)
        self.startup_details.setMinimumHeight(110)
        layout.addWidget(self.startup_details)
        return page

    def set_startup_status(self, message: str) -> None:
        if not self.startup_mode:
            return
        self.startup_status_label.setText(message)
        self.startup_details.append(message)

    def set_startup_progress(self, value: int) -> None:
        if not self.startup_mode:
            return
        bounded = max(0, min(100, value))
        self.startup_progress.setValue(bounded)
        self.startup_percent_label.setText(f"{bounded}%")

    def show_startup_error(self, message: str) -> None:
        if not self.startup_mode:
            return
        self.startup_progress.setRange(0, 100)
        self.startup_progress.setValue(0)
        self.startup_percent_label.setText("—")
        self.startup_status_label.setText("启动失败")
        self.startup_status_label.setStyleSheet(
            "font-size: 16px; font-weight: 600; color: #C64545;"
        )
        self.startup_details.append(message)
        self.startup_details.setVisible(True)

    def finish_startup(self) -> None:
        if not self.startup_mode:
            return
        self.startup_wave.stop()
        self.setWindowTitle("OwVoice - 守望先锋角色语音工具")
        self.page_stack.setCurrentWidget(self.tool_page)
        self.load_voices()

    def build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(236)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        brand = QFrame()
        brand.setObjectName("BrandCard")
        brand.setFixedHeight(86)
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(14, 2, 14, 6)
        brand_layout.setSpacing(1)
        mark = QLabel("✱  OWVOICE")
        mark.setObjectName("BrandMark")
        title = QLabel("OwVoice")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("本地角色配音台")
        subtitle.setObjectName("BrandSubtitle")
        brand_layout.addWidget(mark)
        brand_layout.addWidget(title)
        brand_layout.addWidget(subtitle)
        layout.addWidget(brand)

        role_title = QLabel("角色声音")
        role_title.setObjectName("SectionTitle")
        layout.addWidget(role_title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        role_container = QWidget()
        self.role_layout = QVBoxLayout(role_container)
        self.role_layout.setContentsMargins(0, 0, 2, 0)
        self.role_layout.setSpacing(6)
        self.role_layout.addStretch()
        scroll.setWidget(role_container)
        layout.addWidget(scroll, 1)

        return sidebar

    def build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        kicker = QLabel("✱  LOCAL VOICE STUDIO")
        kicker.setObjectName("PageKicker")
        page_title = QLabel("配音工作台")
        page_title.setObjectName("PageTitle")
        title_box.addWidget(page_title)
        title_box.insertWidget(0, kicker)
        header.addLayout(title_box)
        header.addStretch()
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.status_label)
        layout.addLayout(header)

        voice_card = QFrame()
        voice_card.setObjectName("SectionCard")
        voice_layout = QVBoxLayout(voice_card)
        voice_layout.setContentsMargins(16, 13, 16, 13)
        current_row = QHBoxLayout()
        current_row.addWidget(QLabel("当前角色"))
        self.current_voice_label = QLabel("未选择")
        self.current_voice_label.setObjectName("CurrentVoice")
        current_row.addWidget(self.current_voice_label)
        current_row.addStretch()
        voice_layout.addLayout(current_row)
        layout.addWidget(voice_card)

        text_card = QFrame()
        text_card.setObjectName("TextCard")
        text_layout = QVBoxLayout(text_card)
        text_layout.setContentsMargins(16, 16, 16, 16)
        text_layout.setSpacing(10)
        text_label = QLabel("配音文案")
        text_label.setObjectName("SectionTitle")
        text_layout.addWidget(text_label)
        self.text_input.setMinimumHeight(180)
        text_layout.addWidget(self.text_input, 1)
        layout.addWidget(text_card, 1)

        controls_card = QFrame()
        controls_card.setObjectName("SectionCard")
        controls_layout = QVBoxLayout(controls_card)
        controls_layout.setContentsMargins(16, 13, 16, 13)
        controls_layout.setSpacing(10)

        speed_row = QHBoxLayout()
        speed_label = QLabel("语速")
        speed_label.setObjectName("SpeedLabel")
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_input)
        controls_layout.addLayout(speed_row)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("输出目录"))
        self.output_dir_input = QLineEdit(str(self.output_dir))
        output_row.addWidget(self.output_dir_input, 1)
        self.browse_button = QPushButton("选择目录")
        self.browse_button.setObjectName("SecondaryButton")
        self.browse_button.clicked.connect(self.browse_output_dir)
        output_row.addWidget(self.browse_button)
        controls_layout.addLayout(output_row)

        filename_row = QHBoxLayout()
        filename_row.addWidget(QLabel("WAV 名称"))
        self.filename_input = QLineEdit("last.wav")
        self.filename_input.setPlaceholderText("例如 last.wav")
        filename_row.addWidget(self.filename_input, 1)
        self.open_file_button = QPushButton("打开文件")
        self.open_file_button.setObjectName("SecondaryButton")
        self.open_file_button.clicked.connect(self.open_output_file)
        filename_row.addWidget(self.open_file_button)
        controls_layout.addLayout(filename_row)
        layout.addWidget(controls_card)

        button_row = QHBoxLayout()
        self.generate_button.setFixedHeight(50)
        self.play_button.setFixedHeight(50)
        self.play_button.setObjectName("SecondaryButton")
        open_button = QPushButton("打开输出目录")
        open_button.setObjectName("SecondaryButton")
        open_button.setFixedHeight(50)
        open_button.clicked.connect(self.open_output_folder)
        button_row.addWidget(self.generate_button, 2)
        button_row.addWidget(self.play_button, 1)
        button_row.addWidget(open_button, 1)
        layout.addLayout(button_row)
        return workspace

    def load_voices(self) -> None:
        try:
            api_url = self.api_url()
            health_response = requests.get(f"{api_url}/api/health", timeout=3)
            health_response.raise_for_status()
            health = health_response.json()
            if not health.get("gpt_sovits_online"):
                self.set_status("GPT-SoVITS 未启动", warning=True)
            else:
                self.set_status("引擎在线")
            voices_response = requests.get(f"{api_url}/api/voices", timeout=5)
            voices_response.raise_for_status()
            voices = voices_response.json()
        except Exception as exc:  # noqa: BLE001
            self.set_status("OwVoice 后端未启动", error=True)
            self.generate_button.setEnabled(False)
            QMessageBox.warning(self, "连接失败", f"无法连接 OwVoice 后端：\n{exc}")
            return

        self.voices = voices
        for voice in voices:
            voice_id = str(voice.get("id", ""))
            default_audio = self.output_dir / self.default_filename(
                str(voice.get("display_name", voice_id))
            )
            if voice_id and default_audio.is_file():
                self.last_audio_by_voice[voice_id] = default_audio
        for voice in voices:
            card = VoiceCard(voice)
            card.selected.connect(self.select_voice)
            self.voice_cards[voice["id"]] = card
            self.role_layout.insertWidget(self.role_layout.count() - 1, card)

        if not self.voices:
            self.set_status("没有可用角色", error=True)
            return
        self.generate_button.setEnabled(bool(health.get("gpt_sovits_online")))
        self.select_voice(self.voices[0]["id"])

    def api_url(self) -> str:
        return API_URL

    def check_backend(self) -> None:
        try:
            response = requests.get(f"{self.api_url()}/api/health", timeout=3)
            response.raise_for_status()
            health = response.json()
            if health.get("gpt_sovits_online"):
                self.set_status("GPT-SoVITS 引擎在线")
                self.generate_button.setEnabled(bool(self.voices))
            else:
                self.set_status("GPT-SoVITS 未启动", warning=True)
                self.generate_button.setEnabled(False)
        except Exception as exc:  # noqa: BLE001
            self.set_status("OwVoice 后端未启动", error=True)
            self.generate_button.setEnabled(False)
            QMessageBox.warning(self, "连接失败", f"无法连接 OwVoice 后端：\n{exc}")

    def select_voice(self, voice_id: str) -> None:
        voice = next((item for item in self.voices if item.get("id") == voice_id), None)
        if not voice:
            return
        voice_changed = self.current_voice_id != voice_id
        if voice_changed and self.current_voice_id:
            self.prompt_by_voice[self.current_voice_id] = self.text_input.toPlainText()
        self.current_voice_id = voice_id
        for card_id, card in self.voice_cards.items():
            card.setChecked(card_id == voice_id)
            card.set_selected(card_id == voice_id)

        display_name = voice.get("display_name", voice_id)
        self.current_voice_label.setText(display_name)
        self.filename_input.setText(self.default_filename(display_name))
        if voice_changed:
            self.release_player_source()
            self.last_audio = self.last_audio_by_voice.get(voice_id, Path())
            self.play_button.setEnabled(self.last_audio.is_file())
            prompt = self.prompt_by_voice.get(voice_id)
            if prompt is None:
                prompt = self.default_prompt(voice)
            self.text_input.setPlainText(prompt)

    def default_prompt(self, voice: dict) -> str:
        """固定使用角色训练文本列表中的第一条，兼容旧版单条 prompt_text。"""
        prompts = voice.get("prompt_texts") or []
        if isinstance(prompts, str):
            prompts = [prompts]
        for prompt in prompts:
            prompt = str(prompt).strip()
            if prompt:
                return prompt
        return str(voice.get("prompt_text", "")).strip()

    def _sync_speed_from_slider(self, value: int) -> None:
        self.speed_input.setValue(max(0.10, min(3.00, value / 100)))

    def _sync_speed_from_input(self, value: float) -> None:
        bounded = max(0.10, min(3.00, value))
        self.speed_slider.setValue(round(bounded * 100))

    def _normalize_speed_input(self) -> None:
        self.speed_input.setValue(max(0.10, min(3.00, self.speed_input.value())))

    def generate(self) -> None:
        text = self.text_input.toPlainText().strip()
        voice_id = self.current_voice_id
        save_path = self.get_save_path()
        if not text or not voice_id:
            QMessageBox.warning(self, "提示", "请选择角色并输入配音文案。")
            return
        if not save_path:
            QMessageBox.warning(self, "提示", "请设置输出目录和 WAV 名称。")
            return

        self.filename_input.setText(save_path.name)
        self.pending_audio = save_path
        self.pending_voice_id = voice_id
        self.set_generation_controls_enabled(False)

        self.play_button.setEnabled(False)
        self.set_status("正在合成……", warning=True)
        self.thread = QThread(self)
        request_id = uuid.uuid4().hex
        self.worker = SynthesisWorker(
            self.api_url(),
            voice_id,
            text,
            self.speed_input.value(),
            request_id,
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.success.connect(self.save_audio)
        self.worker.failed.connect(lambda message: self.show_error(message))
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self.set_generation_controls_enabled(True))
        self.thread.start()

    def save_audio(self, data: bytes) -> None:
        if self.pending_audio is None:
            self.show_error("未确定输出文件路径")
            return
        target = self.pending_audio
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            # Windows 下播放器可能仍持有同名文件句柄，必须在替换前释放。
            source_path = self.player.source().toLocalFile()
            if source_path and Path(source_path).resolve() == target.resolve():
                self.release_player_source()

            temp_path.write_bytes(data)
            os.replace(temp_path, target)
        except Exception as exc:  # noqa: BLE001 - 将保存失败显示给用户
            self.show_error(f"保存音频失败：{exc}")
            return
        finally:
            if temp_path.exists():
                temp_path.unlink()

        voice_id = self.pending_voice_id
        if voice_id:
            self.last_audio_by_voice[voice_id] = target
        if voice_id == self.current_voice_id:
            self.last_audio = target
            self.play_button.setEnabled(True)
        self.set_status("合成完成")

    def show_error(self, message: str) -> None:
        self.set_status("合成失败", error=True)
        QMessageBox.critical(self, "合成失败", message)

    def release_player_source(self) -> None:
        """Stop playback and release the current in-memory media source."""
        self.play_when_loaded = False
        self.player.stop()
        self.player.setSource(QUrl())
        if self.playback_buffer is not None:
            self.playback_buffer.close()
            self.playback_buffer.deleteLater()
            self.playback_buffer = None
        self.playback_bytes = b""

    def load_player_source(self, path: Path) -> None:
        """Load a WAV into memory so QMediaPlayer never locks the user file."""
        data = path.read_bytes()
        self.release_player_source()
        self.playback_bytes = data
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            buffer.deleteLater()
            self.playback_bytes = b""
            raise RuntimeError("无法打开内存音频缓冲区")
        self.playback_buffer = buffer
        self.player.setSourceDevice(buffer, QUrl.fromLocalFile(str(path)))

    def toggle_play_pause(self) -> None:
        """播放当前角色最近一次生成的音频；再次点击时暂停或继续。"""
        if not self.last_audio.is_file():
            self.show_error("当前角色还没有可播放的音频")
            return

        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.play_when_loaded = False
            self.player.pause()
            self.set_status("已暂停")
            return

        source_path = self.player.source().toLocalFile()
        if (
            self.playback_buffer is None
            or not source_path
            or Path(source_path).resolve() != self.last_audio.resolve()
        ):
            try:
                self.load_player_source(self.last_audio)
            except OSError as exc:
                self.show_error(f"读取音频失败：{exc}")
                return
            except RuntimeError as exc:
                self.show_error(str(exc))
                return
        self.play_when_loaded = True
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._start_playback()
        else:
            self.set_status("正在加载音频……", warning=True)

    def _start_playback(self) -> None:
        if not self.play_when_loaded:
            return
        self.play_when_loaded = False
        self.player.play()
        self.set_status("正在播放")

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.release_player_source()
            self.set_status("播放完成")
            return
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._start_playback()

    def _on_player_error(self, _error, error_string: str) -> None:
        self.play_when_loaded = False
        self.set_status("播放失败", error=True)
        if error_string:
            QMessageBox.warning(self, "播放失败", error_string)

    def set_status(self, message: str, warning=False, error=False) -> None:
        color = "#C64545" if error else "#CC785C" if warning else "#5DB872"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def set_generation_controls_enabled(self, enabled: bool) -> None:
        self.generate_button.setEnabled(enabled and bool(self.voices))
        self.speed_slider.setEnabled(enabled)
        self.speed_input.setEnabled(enabled)
        self.output_dir_input.setEnabled(enabled)
        self.filename_input.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)
        self.open_file_button.setEnabled(enabled)

    def closeEvent(self, event) -> None:
        self.release_player_source()
        super().closeEvent(event)

    def normalized_filename(self) -> str:
        raw_name = self.filename_input.text().strip()
        if not raw_name:
            return ""
        name = os.path.basename(raw_name)
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
        if not name:
            return ""
        if not name.lower().endswith(".wav"):
            name += ".wav"
        return name

    def default_filename(self, display_name: str) -> str:
        """按当前角色生成默认文件名，用户仍可在输入框中修改。"""
        name = re.sub(r'[<>:"/\\|?*]', "_", display_name).strip(" .")
        return f"{name or 'last'}.wav"

    def get_save_path(self) -> Path | None:
        output_dir = self.output_dir_input.text().strip()
        filename = self.normalized_filename()
        if not output_dir or not filename:
            return None
        return Path(os.path.abspath(os.path.join(output_dir, filename)))

    def browse_output_dir(self) -> None:
        current_dir = self.output_dir_input.text().strip() or str(self.output_dir)
        selected_dir = QFileDialog.getExistingDirectory(self, "选择输出目录", current_dir)
        if selected_dir:
            self.output_dir_input.setText(selected_dir)

    def open_output_folder(self) -> None:
        folder = Path(self.output_dir_input.text().strip() or str(self.output_dir))
        folder = Path(os.path.abspath(str(folder)))
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def open_output_file(self) -> None:
        """在资源管理器中定位当前 WAV；文件尚不存在时打开其所在目录。"""
        target = self.get_save_path() or self.last_audio
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            subprocess.Popen(["explorer.exe", f"/select,{target}"])
        else:
            os.startfile(str(target.parent))


if __name__ == "__main__":
    set_windows_app_identity()
    app = QApplication(sys.argv)
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = OwVoiceApp()
    window.show()
    sys.exit(app.exec())
