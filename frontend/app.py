"""OwVoice 新版桌面前端。只调用 OwVoice API，不直接管理模型。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


API_URL = os.environ.get("OWVOICE_API", "http://127.0.0.1:8765").rstrip("/")
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs"


class SynthesisWorker(QObject):
    success = Signal(bytes)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, voice_id: str, text: str, speed: float):
        super().__init__()
        self.voice_id = voice_id
        self.text = text
        self.speed = speed

    def run(self) -> None:
        try:
            response = requests.post(
                f"{API_URL}/api/synthesize",
                json={"voice_id": self.voice_id, "text": self.text, "speed": self.speed},
                timeout=360,
            )
            if response.status_code != 200:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                raise RuntimeError(detail or f"请求失败：HTTP {response.status_code}")
            self.success.emit(response.content)
        except Exception as exc:  # noqa: BLE001 - 将后端错误显示给用户
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class OwVoiceApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OwVoice - 守望先锋角色语音工具")
        self.resize(820, 620)
        self.thread: QThread | None = None
        self.worker: SynthesisWorker | None = None
        self.last_audio = OUTPUT_DIR / "last_output.wav"

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)

        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self.update_voice_status)
        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("输入需要合成的角色台词……")
        self.text_input.setPlainText("任务开始了。")
        self.status_label = QLabel("正在连接 OwVoice 后端……")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 300)
        self.speed_slider.setValue(100)
        self.speed_label = QLabel("1.00x")
        self.speed_slider.valueChanged.connect(
            lambda value: self.speed_label.setText(f"{value / 100:.2f}x")
        )
        self.generate_button = QPushButton("合成语音")
        self.generate_button.clicked.connect(self.generate)
        self.play_button = QPushButton("播放")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.play_last_audio)

        self.build_ui()
        self.load_voices()

    def build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("OwVoice")
        title.setStyleSheet("font-size: 28px; font-weight: bold;")
        subtitle = QLabel("本地守望先锋角色语音合成")
        subtitle.setStyleSheet("color: #777;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("选择角色："))
        voice_row.addWidget(self.voice_combo, 1)
        voice_row.addWidget(self.status_label, 2)
        layout.addLayout(voice_row)

        layout.addWidget(QLabel("配音文案："))
        layout.addWidget(self.text_input, 1)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("语速："))
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_label)
        layout.addLayout(speed_row)

        button_row = QHBoxLayout()
        button_row.addWidget(self.generate_button, 2)
        button_row.addWidget(self.play_button, 1)
        layout.addLayout(button_row)

        self.setCentralWidget(root)

    def load_voices(self) -> None:
        try:
            health = requests.get(f"{API_URL}/api/health", timeout=3).json()
            if not health.get("gpt_sovits_online"):
                self.status_label.setText("GPT-SoVITS 未启动")
            voices = requests.get(f"{API_URL}/api/voices", timeout=5).json()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText("OwVoice 后端未启动")
            self.generate_button.setEnabled(False)
            QMessageBox.warning(self, "连接失败", f"无法连接 OwVoice 后端：\n{exc}")
            return

        for voice in voices:
            self.voice_combo.addItem(voice.get("display_name", voice["id"]), voice["id"])
        self.update_voice_status()

    def update_voice_status(self) -> None:
        voice_id = self.voice_combo.currentData()
        if voice_id:
            self.status_label.setText(f"当前角色：{voice_id}")

    def generate(self) -> None:
        text = self.text_input.toPlainText().strip()
        voice_id = self.voice_combo.currentData()
        if not text or not voice_id:
            QMessageBox.warning(self, "提示", "请选择角色并输入配音文案。")
            return

        self.generate_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.status_label.setText("正在合成……")
        self.thread = QThread(self)
        self.worker = SynthesisWorker(voice_id, text, self.speed_slider.value() / 100)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.success.connect(self.save_audio)
        self.worker.failed.connect(lambda message: self.show_error(message))
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self.generate_button.setEnabled(True))
        self.thread.start()

    def save_audio(self, data: bytes) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.last_audio.write_bytes(data)
        self.play_button.setEnabled(True)
        self.status_label.setText(f"合成完成：{self.last_audio}")

    def show_error(self, message: str) -> None:
        self.status_label.setText("合成失败")
        QMessageBox.critical(self, "合成失败", message)

    def play_last_audio(self) -> None:
        if self.last_audio.exists():
            from PySide6.QtCore import QUrl

            self.player.setSource(QUrl.fromLocalFile(str(self.last_audio)))
            self.player.play()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OwVoiceApp()
    window.show()
    sys.exit(app.exec())

