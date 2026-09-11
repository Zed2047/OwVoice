"""OwVoice 新版桌面前端。只调用 OwVoice API，不直接管理模型。"""

from __future__ import annotations

import os
import re
import sys
import ctypes
import json
import math
import time
import subprocess
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, QUrl, Qt, QThread, Signal, QSize, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QAbstractItemView,
    QComboBox,
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
    QDialog,
    QGridLayout,
    QHeaderView,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QSizePolicy,
    QSlider,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


API_URL = os.environ.get("OWVOICE_API", "http://127.0.0.1:8765").rstrip("/")
APP_VERSION = os.environ.get("OWVOICE_APP_VERSION", "0.2.0")
PROJECT_DIR = Path(
    os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1])
)
OUTPUT_DIR = PROJECT_DIR / "output"
_ICON_CANDIDATES = [PROJECT_DIR / "assets" / "OwVoice.ico"]
if getattr(sys, "frozen", False):
    _ICON_CANDIDATES.append(Path(getattr(sys, "_MEIPASS", "")) / "assets" / "OwVoice.ico")
ICON_PATH = next((path for path in _ICON_CANDIDATES if path.is_file()), _ICON_CANDIDATES[0])


def _ow_delete_finished_thread(thread: QThread | None) -> None:
    """安全回收已经结束的 Qt 线程；页面引用必须先置空。"""
    if thread is None:
        return
    try:
        thread.deleteLater()
    except RuntimeError:
        # Qt 对象可能已在窗口关闭流程中被回收。
        pass


def _voice_default_prompt(voice: dict) -> str:
    """读取角色模型自带的默认试听文案。"""
    prompts = voice.get("prompt_texts") or []
    if isinstance(prompts, str):
        prompts = [prompts]
    for prompt in prompts:
        prompt = str(prompt).strip()
        if prompt:
            return prompt
    return str(voice.get("prompt_text", "")).strip()


def _sync_voice_prompt_defaults(
    loaded_defaults: dict[str, str],
    prompt_cache: dict[str, str],
    voices: list[dict],
) -> set[str]:
    """模型默认文案变化时，只清除对应角色的旧输入缓存。"""
    changed: set[str] = set()
    current: dict[str, str] = {}
    for voice in voices:
        voice_id = str(voice.get("id", "")).strip()
        if not voice_id:
            continue
        prompt = _voice_default_prompt(voice)
        previous = loaded_defaults.get(voice_id)
        if previous is not None and previous != prompt:
            changed.add(voice_id)
            prompt_cache.pop(voice_id, None)
        current[voice_id] = prompt
    # 保留暂时被删除角色的旧值，重新导入同 ID 模型时仍能识别默认文案变化。
    loaded_defaults.update(current)
    return changed


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
        self.preview_pixmap = QPixmap()
        self.accent = accent
        self.is_selected = False
        self.avatar_size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.preview_pixmap = pixmap
        self.update()

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

        pixmap = self.preview_pixmap if not self.preview_pixmap.isNull() else (QPixmap(self.avatar_path) if self.avatar_path else QPixmap())
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


class TrainingAvatarPicker(AvatarLabel):
    """训练页可点击的圆形头像，未上传时显示默认占位提示。"""

    clicked = Signal()

    def __init__(self, size: int = 92, parent=None):
        super().__init__("点击切换头像", "", accent="#D7D5CE", size=size)
        self.setParent(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击切换头像")

    def set_avatar_pixmap(self, pixmap: QPixmap) -> None:
        self.set_pixmap(pixmap)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        inset = max(5, self.avatar_size // 12)
        inner_size = self.avatar_size - inset * 2
        circle = QPainterPath()
        circle.addEllipse(inset, inset, inner_size, inner_size)
        painter.setClipPath(circle)
        if self.preview_pixmap.isNull():
            painter.fillRect(inset, inset, inner_size, inner_size, QColor("#D7D5CE"))
            painter.setPen(QColor("#5E5D59"))
            font = QFont("Microsoft YaHei", 12)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, "点击\n切换头像")
        else:
            side = min(self.preview_pixmap.width(), self.preview_pixmap.height())
            cropped = self.preview_pixmap.copy(
                (self.preview_pixmap.width() - side) // 2,
                (self.preview_pixmap.height() - side) // 2,
                side,
                side,
            )
            painter.drawPixmap(inset, inset, inner_size, inner_size, cropped)
        painter.setClipping(False)
        painter.setPen(QColor("#CC785C"))
        painter.setBrush(Qt.NoBrush)
        ring_inset = max(3, self.avatar_size // 18)
        painter.drawEllipse(ring_inset, ring_inset, self.avatar_size - ring_inset * 2, self.avatar_size - ring_inset * 2)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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


class ModelEngineUnloadWorker(QObject):
    """进入本地模型库前卸载当前权重，避免模型文件被后端继续占用。"""

    # 参数表示是否实际执行了卸载；没有活动模型时保持幂等，不调用 GPT-SoVITS。
    success = Signal(bool)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_url: str):
        super().__init__()
        self.api_url = api_url

    def run(self) -> None:
        try:
            health = requests.get(f"{self.api_url}/api/health", timeout=3)
            health.raise_for_status()
            health_payload = health.json()
            # 服务在线不代表已经加载了角色权重；没有活动角色时无需调用卸载接口。
            if not health_payload.get("active_voice_id"):
                self.success.emit(False)
                return
            response = requests.post(f"{self.api_url}/api/engine/unload", timeout=660)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400:
                raise RuntimeError(str(payload.get("detail") or response.text or "卸载模型失败"))
            self.success.emit(True)
        except Exception as exc:  # noqa: BLE001 - 将后端错误显示给用户
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ModelCatalogWorker(QObject):
    """后台读取本地模型清单，避免权重目录扫描阻塞 Qt 主线程。"""

    loaded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_url: str):
        super().__init__()
        self.api_url = api_url

    def run(self) -> None:
        try:
            response = requests.get(
                f"{self.api_url}/api/models",
                params={"refresh": "true"},
                timeout=40,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError("模型清单格式无效")
            self.loaded.emit(payload)
        except Exception as exc:  # noqa: BLE001 - 交给界面显示
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class EngineStartWorker(QObject):
    """导入模型后在后台启动 GPT-SoVITS，避免阻塞模型库界面。"""

    ready = Signal(int)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, project_dir: Path, voice: dict):
        super().__init__()
        self.project_dir = project_dir
        self.voice = voice
        self.process: subprocess.Popen | None = None

    def _resolve_path(self, value: object) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path.resolve() if path.is_absolute() else (self.project_dir / path).resolve()

    @staticmethod
    def _online() -> bool:
        try:
            response = requests.get("http://127.0.0.1:9880/control", timeout=2)
            return response.status_code in (200, 404, 405)
        except requests.RequestException:
            return False

    def _stop_process(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except OSError:
            self.process.kill()

    @staticmethod
    def _cancelled() -> bool:
        thread = QThread.currentThread()
        return bool(thread and thread.isInterruptionRequested())

    def run(self) -> None:
        try:
            if self._online():
                raise RuntimeError(
                    "端口 9880 已被其他 GPT-SoVITS/OwVoice 占用。"
                    "请关闭其他 OwVoice 或 GPT-SoVITS 服务后重试。"
                )
            if self._cancelled():
                return

            python_candidates = (
                self.project_dir / ".venv" / "Scripts" / "python.exe",
            )
            python_exe = next((path for path in python_candidates if path.is_file()), None)
            gsv_root = Path(os.environ.get("OWVOICE_GSV_ROOT", self.project_dir / "GPT-SoVITS"))
            api_py = gsv_root / "api.py"
            gpt_path = self._resolve_path(self.voice.get("gpt_model"))
            sovits_path = self._resolve_path(self.voice.get("sovits_model"))
            reference_path = self._resolve_path(self.voice.get("reference_audio"))
            required = (python_exe, api_py, gpt_path, sovits_path, reference_path)
            missing = next((str(path) for path in required if path is None or not path.is_file()), None)
            if missing:
                raise RuntimeError(f"启动 GPT-SoVITS 所需文件不存在：{missing}")

            prompt_text = str(self.voice.get("prompt_text", "")).strip()
            if not prompt_text:
                prompt_texts = self.voice.get("prompt_texts") or []
                if isinstance(prompt_texts, str):
                    prompt_texts = [prompt_texts]
                prompt_text = next((str(item).strip() for item in prompt_texts if str(item).strip()), "")
            if not prompt_text:
                raise RuntimeError("模型没有配置参考文字")

            logs_dir = self.project_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = logs_dir / "gpt_sovits.log"
            stderr_path = logs_dir / "gpt_sovits.error.log"
            environment = os.environ.copy()
            nltk_candidates = (
                self.project_dir / "data" / "nltk_data",
                self.project_dir / ".venv" / "nltk_data",
            )
            nltk_data = next((path for path in nltk_candidates if path.is_dir()), None)
            if nltk_data is not None:
                environment["NLTK_DATA"] = str(nltk_data)
            inference = self.voice.get("inference") or {}
            stdout = stdout_path.open("ab")
            stderr = stderr_path.open("ab")
            try:
                command = [
                    str(python_exe), str(api_py), "-a", "127.0.0.1", "-p", "9880",
                    "-s", str(sovits_path), "-g", str(gpt_path), "-dr", str(reference_path),
                    "-dt", prompt_text, "-dl", str(self.voice.get("prompt_language", "zh")),
                    "-cp", str(inference.get("cut_punc", "，。？！；：,.?!…")),
                ]
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self.process = subprocess.Popen(
                    command,
                    cwd=str(gsv_root),
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=creationflags,
                    env=environment,
                )
            finally:
                stdout.close()
                stderr.close()

            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if self._cancelled():
                    self._stop_process()
                    return
                if self.process is not None and self.process.poll() is not None:
                    detail = "GPT-SoVITS 启动后立即退出。"
                    try:
                        log = stderr_path.read_text(encoding="utf-8", errors="replace")[-1800:].strip()
                    except OSError:
                        log = ""
                    if log:
                        detail += f"\n\n日志尾部：\n{log}"
                    raise RuntimeError(detail)
                if self._online():
                    self.ready.emit(self.process.pid if self.process else 0)
                    return
                time.sleep(0.5)
            raise RuntimeError("等待 GPT-SoVITS 启动超时，请检查 logs 目录。")
        except Exception as exc:  # noqa: BLE001 - 交给主界面显示
            self._stop_process()
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ModelActionWorker(QObject):
    """在后台执行模型广场的下载或删除，避免阻塞桌面界面。"""

    success = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_url: str, model_id: str, action: str, source_dir: str | None = None, new_name: str | None = None):
        super().__init__()
        self.api_url = api_url
        self.model_id = model_id
        self.action = action
        self.source_dir = source_dir
        self.new_name = new_name

    def run(self) -> None:
        try:
            if self.action == "install":
                response = requests.post(f"{self.api_url}/api/models/{self.model_id}/install", timeout=3600)
            elif self.action == "update":
                response = requests.post(f"{self.api_url}/api/models/{self.model_id}/update", timeout=3600)
            elif self.action == "import":
                response = requests.post(
                    f"{self.api_url}/api/models/import",
                    json={"source_dir": self.source_dir or ""},
                    timeout=3600,
                )
            elif self.action == "rename":
                response = requests.post(
                    f"{self.api_url}/api/models/{self.model_id}/rename",
                    json={"name": self.new_name or ""},
                    timeout=30,
                )
            else:
                response = requests.delete(f"{self.api_url}/api/models/{self.model_id}", timeout=30)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400:
                raise RuntimeError(str(payload.get("detail") or response.text or "模型操作失败"))
            if self.action == "import":
                imported = payload.get("models") or ([] if payload.get("model") is None else [payload.get("model")])
                skipped = payload.get("skipped") or []
                failed = payload.get("failed") or []
                parts = [f"新导入 {len(imported)} 个"]
                if skipped:
                    parts.append(f"已存在 {len(skipped)} 个")
                if failed:
                    parts.append(f"失败 {len(failed)} 个")
                message = "本地模型处理完成：" + "，".join(parts)
            else:
                message = {
                    "install": "模型安装完成",
                    "update": "模型更新完成",
                    "rename": "模型重命名完成",
                }.get(self.action, "已从模型库清单移除，文件未删除")
            self.success.emit(message)
        except Exception as exc:  # noqa: BLE001 - 将后端错误显示给用户
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ModelPlazaDialog(QDialog):
    """模型广场：只通过本地后端操作，模型文件始终保存在项目目录。"""

    models_changed = Signal()

    def __init__(self, api_url: str, parent=None):
        super().__init__(parent)
        self.api_url = api_url
        self.models: list[dict] = []
        self.action_thread: QThread | None = None
        self.action_worker: ModelActionWorker | None = None
        self.setWindowTitle("模型广场")
        self.setObjectName("ModelPlazaDialog")
        self.resize(620, 430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("模型广场")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        intro = QLabel("按需下载角色模型，安装后会自动出现在左侧角色列表。")
        intro.setObjectName("ModelPlazaIntro")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status_label = QLabel("正在读取模型清单……")
        self.status_label.setObjectName("ModelPlazaStatus")
        layout.addWidget(self.status_label)
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("ModelPlazaList")
        self.list_widget.setSpacing(4)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("刷新清单")
        self.refresh_button.setObjectName("CatalogRefreshButton")
        self.refresh_button.clicked.connect(self.refresh_catalog)
        self.install_button = QPushButton("安装角色")
        self.install_button.setObjectName("ModelInstallButton")
        self.install_button.clicked.connect(self.install_selected)
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("ModelRenameButton")
        self.rename_button.clicked.connect(self.rename_selected)
        self.delete_button = QPushButton("移除")
        self.delete_button.setObjectName("ModelDeleteButton")
        self.delete_button.clicked.connect(self.delete_selected)
        close_button = QPushButton("关闭")
        close_button.setObjectName("ModelCloseButton")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch()
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        self._update_buttons()
        QTimer.singleShot(0, self.refresh_catalog)

    @staticmethod
    def _size_text(size: object) -> str:
        try:
            value = float(size or 0)
        except (TypeError, ValueError):
            value = 0
        if value >= 1024 ** 3:
            return f"{value / 1024 ** 3:.1f} GB"
        if value >= 1024 ** 2:
            return f"{value / 1024 ** 2:.0f} MB"
        return "大小未知"

    def refresh_catalog(self) -> None:
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在刷新模型清单……")
        try:
            response = requests.get(f"{self.api_url}/api/models", params={"refresh": "true"}, timeout=40)
            response.raise_for_status()
            self.models = response.json()
            self.list_widget.clear()
            for model in self.models:
                name = model.get("name") or model.get("id", "未知模型")
                version = model.get("version") or model.get("installedVersion") or "未知版本"
                installed = bool(model.get("installed"))
                installed_version = model.get("installedVersion")
                if installed and installed_version and str(installed_version) != str(version):
                    state = f"已安装 {installed_version}，可更新至 {version}"
                elif installed:
                    state = "已安装"
                else:
                    state = "可下载"
                item = QListWidgetItem()
                item.setData(Qt.UserRole, model.get("id"))
                item.setSizeHint(QSize(0, 68))
                self.list_widget.addItem(item)
                row = QWidget()
                row.setObjectName("ModelRow")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(14, 8, 14, 8)
                row_layout.setSpacing(12)
                text_box = QVBoxLayout()
                text_box.setSpacing(2)
                name_label = QLabel(name)
                name_label.setObjectName("ModelRowTitle")
                meta_label = QLabel(f"版本 v{version}  ·  {self._size_text(model.get('size'))}")
                meta_label.setObjectName("ModelRowMeta")
                text_box.addWidget(name_label)
                text_box.addWidget(meta_label)
                state_label = QLabel(state)
                state_label.setObjectName("ModelRowStateInstalled" if installed else "ModelRowState")
                row_layout.addLayout(text_box, 1)
                row_layout.addWidget(state_label, 0, Qt.AlignVCenter)
                self.list_widget.setItemWidget(item, row)
            self.status_label.setText(f"模型清单已更新，共 {len(self.models)} 个模型")
            self._update_buttons()
        except Exception as exc:  # noqa: BLE001 - 显示网络或后端错误
            self.status_label.setText(f"清单刷新失败：{exc}")
        finally:
            self.refresh_button.setEnabled(True)

    def _selected_model(self) -> dict | None:
        row = self.list_widget.currentRow()
        return self.models[row] if 0 <= row < len(self.models) else None

    def _update_buttons(self, *_args) -> None:
        model = self._selected_model()
        installed = bool(model and model.get("installed"))
        version = model.get("version") if model else None
        installed_version = model.get("installedVersion") if model else None
        needs_update = bool(installed and version and installed_version and str(version) != str(installed_version))
        self.install_button.setText("更新角色" if needs_update else "安装角色")
        self.install_button.setEnabled(bool(model and (not installed or needs_update) and not self.action_thread))
        self.rename_button.setEnabled(bool(installed and not self.action_thread))
        self.delete_button.setEnabled(bool(installed and not self.action_thread))

    def install_selected(self) -> None:
        model = self._selected_model()
        if model:
            installed = bool(model.get("installed"))
            version = model.get("version")
            installed_version = model.get("installedVersion")
            action = "update" if installed and version and installed_version and str(version) != str(installed_version) else "install"
            self._run_action(str(model.get("id")), action)

    def delete_selected(self) -> None:
        model = self._selected_model()
        if not model:
            return
        answer = QMessageBox.question(self, "确认移出", f"确定将“{model.get('name', model.get('id'))}”移出模型库吗？\n模型文件不会被删除。")
        if answer == QMessageBox.Yes:
            self._run_action(str(model.get("id")), "delete")

    def rename_selected(self) -> None:
        model = self._selected_model()
        if not model or not model.get("installed") or self.action_thread:
            return
        current_name = str(model.get("display_name") or model.get("name") or model.get("id") or "")
        dialog = QDialog(self)
        dialog.setWindowTitle("重命名模型")
        dialog.setObjectName("ModelRenameDialog")
        dialog.setFixedWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        title = QLabel("重命名模型")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        current = QLabel(f"当前名称：{current_name}")
        current.setObjectName("ModelRenameCurrent")
        layout.addWidget(current)
        layout.addWidget(QLabel("新名称"))
        name_input = QLineEdit(current_name)
        name_input.setObjectName("ModelRenameInput")
        name_input.setFixedWidth(240)
        name_input.selectAll()
        layout.addWidget(name_input, 0, Qt.AlignLeft)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("ModelCloseButton")
        cancel.clicked.connect(dialog.reject)
        confirm = QPushButton("确定")
        confirm.setObjectName("ModelRenameButton")
        confirm.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        new_name = name_input.text().strip()
        if new_name and new_name != current_name:
            self._run_action(str(model.get("id")), "rename", new_name=new_name)

    def _run_action(self, model_id: str, action: str, new_name: str | None = None) -> None:
        self.action_thread = QThread(self)
        self.action_worker = ModelActionWorker(self.api_url, model_id, action, new_name=new_name)
        self.action_worker.moveToThread(self.action_thread)
        self.action_thread.started.connect(self.action_worker.run)
        self.action_worker.success.connect(self._action_success)
        self.action_worker.failed.connect(self._action_failed)
        self.action_worker.finished.connect(self.action_thread.quit)
        self.action_worker.finished.connect(self.action_worker.deleteLater)
        self.action_thread.finished.connect(self._action_finished)
        # 线程对象由页面持有到页面销毁；不要在 finished 信号中提前 deleteLater，
        # 否则页面仍可能保留失效的 QThread 引用，关闭窗口时会触发 native crash。
        self.install_button.setEnabled(False)
        self.rename_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在处理模型，请耐心等待……")
        self.action_thread.start()

    def _action_success(self, message: str) -> None:
        self.status_label.setText(message)
        self.models_changed.emit()
        QTimer.singleShot(50, self.refresh_catalog)

    def _action_failed(self, message: str) -> None:
        self.status_label.setText(f"模型操作失败：{message}")
        QMessageBox.warning(self, "模型操作失败", message)

    def _action_finished(self) -> None:
        finished_thread = self.action_thread
        self.action_thread = None
        self.action_worker = None
        _ow_delete_finished_thread(finished_thread)
        self._update_buttons()

class ModelPreviewWorker(QObject):
    """后台加载模型广场头像，避免打开页面时卡住主界面。"""

    loaded = Signal(str, bytes)
    finished = Signal()

    def __init__(self, previews: list[tuple[str, str]]):
        super().__init__()
        self.previews = previews

    def run(self) -> None:
        try:
            for model_id, url in self.previews:
                try:
                    response = requests.get(url, timeout=12)
                    response.raise_for_status()
                    self.loaded.emit(model_id, response.content)
                except requests.RequestException:
                    continue
        finally:
            self.finished.emit()


class TrainingApiWorker(QObject):
    """在后台线程执行训练 API 请求，避免上传或查询阻塞桌面窗口。"""

    loaded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, method: str, url: str, payload: dict | None = None, headers: dict[str, str] | None = None):
        super().__init__()
        self.method = method
        self.url = url
        self.payload = payload
        self.headers = headers or {}

    def run(self) -> None:
        try:
            response = requests.request(
                self.method,
                self.url,
                json=self.payload,
                headers=self.headers,
                timeout=900 if self.method == "POST" else 120,
            )
            try:
                data = response.json()
            except ValueError:
                data = {"detail": response.text.strip()}
            if response.status_code >= 400:
                detail = data.get("detail", response.text) if isinstance(data, dict) else response.text
                raise RuntimeError(str(detail))
            self.loaded.emit(data)
        except Exception as exc:  # noqa: BLE001 - 转成界面可读错误
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class TrainingSetupWorker(QObject):
    """后台执行一次性训练环境安装，避免 pip/模型下载阻塞桌面窗口。"""

    output = Signal(str)
    success = Signal()
    failed = Signal(str)
    finished = Signal()

    def run(self) -> None:
        process = None
        output_lines: list[str] = []
        try:
            powershell = "powershell.exe"
            script = PROJECT_DIR / "scripts" / "setup_training.ps1"
            if not script.is_file():
                raise RuntimeError(f"找不到训练安装脚本：{script}")
            command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip()
                if text:
                    output_lines.append(text)
                    if len(output_lines) > 40:
                        output_lines.pop(0)
                    self.output.emit(text)
            return_code = process.wait()
            if return_code != 0:
                details = "\n".join(output_lines[-12:])
                suffix = f"\n\n脚本最后输出：\n{details}" if details else ""
                raise RuntimeError(f"训练环境安装脚本退出，代码：{return_code}{suffix}")
            self.success.emit()
        except Exception as exc:  # noqa: BLE001 - 转成界面可读错误
            self.failed.emit(str(exc))
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            self.finished.emit()


class TrainingPage(QWidget):
    """OwVoice 内置的本地训练向导。"""

    back_requested = Signal()
    model_saved = Signal()

    def __init__(self, api_url: str, parent=None):
        super().__init__(parent)
        self.api_url = api_url
        self.session_id = os.environ.get("OWVOICE_SESSION_ID", "").strip() or uuid.uuid4().hex
        self.job_id: str | None = None
        self.source_paths: list[str] = []
        self.request_thread: QThread | None = None
        self.request_worker: TrainingApiWorker | None = None
        self.training_setup_thread: QThread | None = None
        self.training_setup_worker: TrainingSetupWorker | None = None
        self.training_environment_ready = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._poll_status)
        self._table_dirty = False
        self.job_status = ""
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("TrainingPage")
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(12, 4, 12, 4)
        page_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("TrainingScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("TrainingContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 12)
        layout.setSpacing(14)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)

        header = QHBoxLayout()
        self.back_button = QPushButton("返回")
        self.back_button.setObjectName("TrainingBackButton")
        self.back_button.clicked.connect(self._go_back)
        header.addWidget(self.back_button, 0, Qt.AlignTop)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("训练一个新声音")
        title.setObjectName("TrainingPageTitle")
        title.setWordWrap(True)
        subtitle = QLabel("上传语音，校对文字，OwVoice 会自动完成数据准备和模型训练。")
        subtitle.setObjectName("TrainingPageSubtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        layout.addLayout(header)

        self.step_label = QLabel("1  准备素材   →   2  校对文本   →   3  训练   →   4  保存")
        self.step_label.setObjectName("TrainingStepLabel")
        self.step_label.setWordWrap(True)
        layout.addWidget(self.step_label)

        info = QFrame()
        info.setObjectName("TrainingCard")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(18, 16, 18, 16)
        top = QVBoxLayout()
        top.setSpacing(6)
        name_label = QLabel("模型名称")
        name_label.setObjectName("TrainingFieldLabel")
        top.addWidget(name_label)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：我的新声音")
        self.name_input.setText("我的新声音")
        self.name_input.setObjectName("TrainingNameInput")
        top.addWidget(self.name_input)
        language_label = QLabel("训练语言")
        language_label.setObjectName("TrainingFieldLabel")
        top.addWidget(language_label)
        self.language_combo = QComboBox()
        self.language_combo.addItem("中文", "zh")
        self.language_combo.addItem("粤语", "yue")
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("日本語", "ja")
        self.language_combo.addItem("한국어", "ko")
        top.addWidget(self.language_combo)
        info_layout.addLayout(top)

        self.choose_button = QPushButton("选择语音文件")
        self.choose_button.setObjectName("TrainingPrimaryButton")
        self.choose_button.clicked.connect(self._choose_audio)
        info_layout.addWidget(self.choose_button, 0, Qt.AlignLeft)
        self.file_list = QListWidget()
        self.file_list.setObjectName("TrainingFileList")
        self.file_list.setMinimumHeight(78)
        self.file_list.setMaximumHeight(170)
        self.file_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        info_layout.addWidget(self.file_list)
        self.file_hint = QLabel("支持 WAV、MP3、FLAC、M4A 等常见格式；建议使用同一个人的清晰语音。")
        self.file_hint.setObjectName("TrainingHint")
        self.file_hint.setWordWrap(True)
        info_layout.addWidget(self.file_hint)
        layout.addWidget(info)

        text_card = QFrame()
        text_card.setObjectName("TrainingCard")
        text_layout = QVBoxLayout(text_card)
        text_layout.setContentsMargins(18, 16, 18, 16)
        text_head = QHBoxLayout()
        text_title = QLabel("校对训练文本")
        text_title.setObjectName("TrainingSectionTitle")
        text_head.addWidget(text_title)
        text_head.addStretch()
        self.transcribe_button = QPushButton("自动识别文字")
        self.transcribe_button.setObjectName("TrainingSecondaryButton")
        self.transcribe_button.clicked.connect(self._transcribe)
        self.save_text_button = QPushButton("保存文本")
        self.save_text_button.setObjectName("TrainingSecondaryButton")
        self.save_text_button.clicked.connect(self._save_text)
        text_head.addWidget(self.transcribe_button)
        text_head.addWidget(self.save_text_button)
        text_layout.addLayout(text_head)
        hint = QLabel("自动识别只是初稿，请逐条检查；模型会根据“音频 + 对应文字”学习。")
        hint.setObjectName("TrainingHint")
        hint.setWordWrap(True)
        text_layout.addWidget(hint)
        self.transcript_table = QTableWidget(0, 2)
        self.transcript_table.setObjectName("TrainingTranscriptTable")
        self.transcript_table.setHorizontalHeaderLabels(["音频", "训练文本（双击修改）"])
        self.transcript_table.horizontalHeader().setStretchLastSection(True)
        self.transcript_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.transcript_table.verticalHeader().setDefaultSectionSize(42)
        self.transcript_table.verticalHeader().setMinimumSectionSize(42)
        self.transcript_table.setWordWrap(True)
        self.transcript_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.transcript_table.itemChanged.connect(lambda _item: setattr(self, "_table_dirty", True))
        self.transcript_table.setMinimumHeight(190)
        text_layout.addWidget(self.transcript_table)
        layout.addWidget(text_card, 1)

        action_card = QFrame()
        action_card.setObjectName("TrainingActionCard")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(18, 14, 18, 14)
        action_layout.setSpacing(10)

        status_row = QHBoxLayout()
        self.status_label = QLabel("请选择语音文件开始。")
        self.status_label.setObjectName("TrainingStatusLabel")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setObjectName("TrainingProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(120)
        self.progress.setMaximumWidth(260)
        self.progress.setMinimumHeight(8)
        self.progress.setMaximumHeight(10)
        status_row.addWidget(self.progress, 1)
        action_layout.addLayout(status_row)

        bottom = QGridLayout()
        self.prepare_button = QPushButton("准备训练数据")
        self.prepare_button.setObjectName("TrainingSecondaryButton")
        self.prepare_button.setEnabled(False)
        self.prepare_button.clicked.connect(self._prepare)
        bottom.addWidget(self.prepare_button, 0, 0)
        self.train_button = QPushButton("开始训练")
        self.train_button.setObjectName("TrainingPrimaryButton")
        self.train_button.setEnabled(False)
        self.train_button.clicked.connect(self._start_training)
        bottom.addWidget(self.train_button, 0, 1)
        self.save_model_button = QPushButton("保存到本地模型库")
        self.save_model_button.setObjectName("TrainingPrimaryButton")
        self.save_model_button.setEnabled(False)
        self.save_model_button.clicked.connect(self._finalize)
        bottom.addWidget(self.save_model_button, 1, 0)
        self.cancel_button = QPushButton("停止任务")
        self.cancel_button.setObjectName("TrainingSecondaryButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        bottom.addWidget(self.cancel_button, 1, 1)
        bottom.setColumnStretch(0, 1)
        bottom.setColumnStretch(1, 1)
        action_layout.addLayout(bottom)
        layout.addWidget(action_card)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("TrainingLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(72)
        self.log_view.setMaximumHeight(150)
        layout.addWidget(self.log_view)

    def _choose_audio(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择训练语音",
            str(PROJECT_DIR),
            "音频文件 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.wma)",
        )
        if not paths:
            return
        self.job_id = None
        self._table_dirty = False
        self.transcript_table.setRowCount(0)
        self.source_paths = paths
        self.file_list.clear()
        for path in paths:
            self.file_list.addItem(Path(path).name)
        self.file_hint.setText(f"已选择 {len(paths)} 个文件，正在复制到本地训练任务……")
        self._create_job()

    def _request(self, method: str, path: str, payload: dict | None, callback) -> None:
        if self.request_thread is not None:
            return
        self.request_thread = QThread(self)
        self.request_worker = TrainingApiWorker(
            method,
            f"{self.api_url}{path}",
            payload,
            {"X-OwVoice-Session": self.session_id},
        )
        self.request_worker.moveToThread(self.request_thread)
        self.request_thread.started.connect(self.request_worker.run)
        self.request_worker.loaded.connect(callback)
        self.request_worker.failed.connect(self._request_failed)
        self.request_worker.finished.connect(self.request_thread.quit)
        self.request_worker.finished.connect(self.request_worker.deleteLater)
        self.request_thread.finished.connect(self._request_finished)
        # request_thread 由 TrainingPage 的 closeEvent 统一回收，避免失效引用。
        self.request_thread.start()

    def _request_finished(self) -> None:
        self.request_thread = None
        self.request_worker = None
        self._set_buttons()

    def _request_failed(self, message: str) -> None:
        self.status_label.setText(f"操作失败：{message}")
        self.log_view.append(message)
        self._set_buttons()

    def _create_job(self) -> None:
        if not self.source_paths:
            return
        self.status_label.setText("正在复制音频并创建训练任务……")
        payload = {"name": self.name_input.text().strip(), "language": self.language_combo.currentData(), "source_paths": self.source_paths}
        self._request("POST", "/api/training/jobs", payload, self._job_created)

    def _job_created(self, job: object) -> None:
        if not isinstance(job, dict):
            return
        self.job_id = str(job.get("id", "")) or None
        self._render_job(job)
        self.log_view.append(str(job.get("message", "训练任务已创建。")))
        self.status_label.setText("素材已上传，请先自动识别或填写文本。")
        self._set_buttons()

    def _render_job(self, job: dict) -> None:
        self.job_status = str(job.get("status", ""))
        files = job.get("files", [])
        if self.transcript_table.rowCount() != len(files):
            self.transcript_table.setRowCount(len(files))
            for row, item in enumerate(files):
                name = QTableWidgetItem(str(item.get("name", "")))
                name.setFlags(name.flags() & ~Qt.ItemIsEditable)
                self.transcript_table.setItem(row, 0, name)
                self.transcript_table.setItem(row, 1, QTableWidgetItem(str(item.get("text", ""))))
        elif not self._table_dirty:
            for row, item in enumerate(files):
                cell = self.transcript_table.item(row, 1)
                if cell:
                    cell.setText(str(item.get("text", "")))
        progress = int(job.get("progress", 0) or 0)
        self.progress.setValue(max(0, min(100, progress)))
        self.status_label.setText(str(job.get("message", job.get("stage", ""))))
        if str(job.get("status", "")) in {"completed", "registered"}:
            self.log_view.append(str(job.get("message", "训练完成。")))

    def _save_text(self, after=None) -> None:
        if not self.job_id:
            self.status_label.setText("请先选择并上传语音文件。")
            return
        transcripts = []
        for row in range(self.transcript_table.rowCount()):
            name_item = self.transcript_table.item(row, 0)
            text_item = self.transcript_table.item(row, 1)
            transcripts.append({"name": name_item.text() if name_item else "", "text": text_item.text().strip() if text_item else ""})
        self.status_label.setText("正在保存训练文本……")
        callback = lambda data: self._text_saved(data, after)
        self._request("PUT", f"/api/training/jobs/{self.job_id}/transcripts", {"transcripts": transcripts}, callback)

    def _text_saved(self, job: object, after=None) -> None:
        self._table_dirty = False
        if isinstance(job, dict):
            self._render_job(job)
        self._table_dirty = False
        if after:
            QTimer.singleShot(0, after)

    def _transcribe(self) -> None:
        if not self.job_id:
            self.status_label.setText("请先上传语音文件。")
            return
        self.status_label.setText("正在启动自动识别……")
        self._request("POST", f"/api/training/jobs/{self.job_id}/transcribe", None, self._action_started)

    def _prepare(self) -> None:
        if self._table_dirty:
            self._save_text(lambda: self._start_prepare_request())
        else:
            self._start_prepare_request()

    def _start_prepare_request(self) -> None:
        if not self.job_id:
            return
        self.status_label.setText("正在启动数据准备……")
        self._request("POST", f"/api/training/jobs/{self.job_id}/prepare", None, self._action_started)

    def _start_training(self) -> None:
        if not self.job_id:
            return
        self.status_label.setText("正在启动训练……")
        self._request("POST", f"/api/training/jobs/{self.job_id}/start", None, self._action_started)

    def _action_started(self, job: object) -> None:
        if isinstance(job, dict):
            self._render_job(job)
        if self.job_id:
            self.poll_timer.start()
        self._set_buttons()

    def _poll_status(self) -> None:
        if self.job_id and self.request_thread is None:
            self._request("GET", f"/api/training/jobs/{self.job_id}", None, self._status_loaded)

    def _status_loaded(self, job: object) -> None:
        if not isinstance(job, dict):
            return
        self._render_job(job)
        status = str(job.get("status", ""))
        if status in {"failed", "registered", "draft", "prepared", "completed"}:
            self.poll_timer.stop()
            self._set_buttons()

    def _finalize(self) -> None:
        if not self.job_id:
            return
        self.status_label.setText("正在整理模型文件并保存到本地模型库……")
        self._request("POST", f"/api/training/jobs/{self.job_id}/finalize", None, self._finalized)

    def _finalized(self, job: object) -> None:
        if isinstance(job, dict):
            self._render_job(job)
        self.poll_timer.stop()
        self.model_saved.emit()
        self._set_buttons()

    def _set_buttons(self) -> None:
        busy = self.request_thread is not None
        has_job = bool(self.job_id)
        running = self.job_status in {"transcribing", "preparing", "running", "cancelling"}
        self.choose_button.setEnabled(not busy)
        self.transcribe_button.setEnabled(has_job and not busy and not running)
        self.save_text_button.setEnabled(has_job and not busy and not running)
        self.prepare_button.setEnabled(has_job and not busy and not running and self.progress.value() < 70)
        self.train_button.setEnabled(has_job and not busy and not running and self.progress.value() >= 70 and self.progress.value() < 100)
        self.save_model_button.setEnabled(has_job and not busy and not running and self.job_status == "completed")
        self.cancel_button.setEnabled(has_job and not busy and running)

    def _cancel(self) -> None:
        if not self.job_id or self.request_thread is not None:
            return
        self._request("POST", f"/api/training/jobs/{self.job_id}/cancel", None, self._action_started)

    def _go_back(self) -> None:
        if self.job_status in {"transcribing", "preparing", "running", "cancelling"}:
            self._ow_training_confirm_stop("back")
            return
        if self.poll_timer.isActive() or self.request_thread is not None:
            if QMessageBox.question(self, "返回", "训练任务仍在运行，确定返回吗？") != QMessageBox.Yes:
                return
        self.back_requested.emit()


class ModelPlazaPage(QWidget):
    """主窗口内的模型广场页面。"""

    models_changed = Signal()
    back_requested = Signal()

    def __init__(self, api_url: str, parent=None):
        super().__init__(parent)
        self.api_url = api_url
        self.models: list[dict] = []
        self.action_thread: QThread | None = None
        self.action_worker: ModelActionWorker | None = None
        self.catalog_thread: QThread | None = None
        self.catalog_worker: ModelCatalogWorker | None = None
        self.preview_thread: QThread | None = None
        self.preview_worker: ModelPreviewWorker | None = None
        self.avatar_labels: dict[str, AvatarLabel] = {}
        self.setObjectName("ModelPlazaPage")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(18)
        self.back_button = QPushButton("返回")
        self.back_button.setObjectName("ModelBackButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        header.addWidget(self.back_button, 0, Qt.AlignTop)
        title_box = QVBoxLayout()
        title_box.setSpacing(5)
        title = QLabel("模型广场")
        title.setObjectName("ModelPlazaPageTitle")
        subtitle = QLabel("选择需要的角色，下载后即可在左侧角色列表中使用。")
        subtitle.setObjectName("ModelPlazaPageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.refresh_button = QPushButton("刷新模型清单")
        self.refresh_button.setObjectName("ModelRefreshButton")
        self.refresh_button.clicked.connect(self.refresh_catalog)
        header.addWidget(self.refresh_button, 0, Qt.AlignTop)
        layout.addLayout(header)

        self.status_label = QLabel("准备读取模型清单……")
        self.status_label.setObjectName("ModelPlazaPageStatus")
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("ModelPlazaPageList")
        self.list_widget.setSpacing(7)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self.list_widget, 1)

        footer = QHBoxLayout()
        self.install_button = QPushButton("安装角色")
        self.install_button.setObjectName("ModelInstallButton")
        self.install_button.clicked.connect(self.install_selected)
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("ModelRenameButton")
        self.rename_button.clicked.connect(self.rename_selected)
        self.delete_button = QPushButton("移除")
        self.delete_button.setObjectName("ModelDeleteButton")
        self.delete_button.clicked.connect(self.delete_selected)
        footer.addStretch()
        footer.addWidget(self.install_button)
        footer.addWidget(self.rename_button)
        footer.addWidget(self.delete_button)
        layout.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        self._update_buttons()

    @staticmethod
    def _size_text(size: object) -> str:
        try:
            value = float(size or 0)
        except (TypeError, ValueError):
            value = 0
        if value >= 1024 ** 3:
            return f"{value / 1024 ** 3:.1f} GB"
        if value >= 1024 ** 2:
            return f"{value / 1024 ** 2:.0f} MB"
        return "大小未知"

    @staticmethod
    def _local_avatar(model: dict) -> str:
        model_id = str(model.get("id", "")).strip()
        value = str(model.get("avatar", "") or model.get("preview", "")).strip()
        if not value or value.startswith(("http://", "https://")):
            return ""
        candidate = Path(value)
        if candidate.is_absolute():
            candidates = [candidate]
        else:
            candidates = [PROJECT_DIR / candidate]
            # 模型目录可能已经按“显示名称__内部 ID”重命名，必须依据注册表
            # 的 path 定位，不能只用内部 ID 拼接目录。
            registered_path = Path(str(model.get("path", "")).strip())
            if registered_path.parts[:1] == ("models",):
                registered_path = Path(*registered_path.parts[1:])
            if registered_path.parts:
                candidates.append(PROJECT_DIR / "data" / "models" / registered_path / candidate)
            # 兼容旧版注册表或后端尚未返回 path 的情况。
            if model_id:
                candidates.append(PROJECT_DIR / "data" / "models" / model_id / candidate)
        for path in candidates:
            if path.is_file():
                return str(path)
        return ""

    @staticmethod
    def _remote_avatar(model: dict) -> str:
        for key in ("avatarUrl", "previewUrl"):
            value = str(model.get(key, "") or "").strip()
            if value.startswith(("http://", "https://")):
                return value
        value = str(model.get("avatar", "") or "").strip()
        return value if value.startswith(("http://", "https://")) else ""

    def refresh_catalog(self) -> None:
        if self.catalog_thread is not None:
            return
        self.refresh_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.status_label.setText("正在刷新模型清单……")
        self.catalog_thread = QThread(self)
        self.catalog_worker = ModelCatalogWorker(self.api_url)
        self.catalog_worker.moveToThread(self.catalog_thread)
        self.catalog_thread.started.connect(self.catalog_worker.run)
        self.catalog_worker.loaded.connect(self._render_catalog)
        self.catalog_worker.failed.connect(self._catalog_failed)
        self.catalog_worker.finished.connect(self.catalog_thread.quit)
        self.catalog_worker.finished.connect(self.catalog_worker.deleteLater)
        self.catalog_thread.finished.connect(self._catalog_finished)
        # catalog_thread 由页面生命周期统一回收，避免 finished 后留下失效引用。
        self.catalog_thread.start()

    def _render_catalog(self, models: object) -> None:
        self.models = [item for item in models if isinstance(item, dict)]
        previous_id = self.list_widget.currentItem().data(Qt.UserRole) if self.list_widget.currentItem() else None
        self.list_widget.clear()
        self.avatar_labels.clear()
        previews: list[tuple[str, str]] = []
        restore_row = -1
        for model in self.models:
            model_id = str(model.get("id", ""))
            name = str(model.get("display_name") or model.get("name") or model_id or "未知模型")
            version = str(model.get("version") or model.get("installedVersion") or "未知版本")
            installed = bool(model.get("installed"))
            installed_version = model.get("installedVersion")
            needs_update = bool(installed and installed_version and str(installed_version) != version)
            state = "可更新" if needs_update else ("已安装" if installed else "未下载")
            state_object = "ModelCardStateInstalled" if installed and not needs_update else ("ModelCardStateUpdate" if needs_update else "ModelCardState")
            item = QListWidgetItem()
            item.setData(Qt.UserRole, model_id)
            item.setSizeHint(QSize(0, 104))
            self.list_widget.addItem(item)
            row = QWidget()
            row.setObjectName("ModelCard")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(16, 12, 18, 12)
            row_layout.setSpacing(16)
            avatar = AvatarLabel(name, self._local_avatar(model), accent="#CC785C", size=64)
            self.avatar_labels[model_id] = avatar
            row_layout.addWidget(avatar, 0, Qt.AlignVCenter)
            text_box = QVBoxLayout()
            text_box.setSpacing(3)
            name_label = QLabel(name)
            name_label.setObjectName("ModelCardTitle")
            meta_label = QLabel(f"v{version}  ·  {self._size_text(model.get('size'))}")
            meta_label.setObjectName("ModelCardMeta")
            text_box.addWidget(name_label)
            text_box.addWidget(meta_label)
            row_layout.addLayout(text_box, 1)
            state_label = QLabel(state)
            state_label.setObjectName(state_object)
            row_layout.addWidget(state_label, 0, Qt.AlignVCenter)
            self.list_widget.setItemWidget(item, row)
            if previous_id == model_id:
                restore_row = self.list_widget.count() - 1
            remote_avatar = self._remote_avatar(model)
            if remote_avatar and avatar.preview_pixmap.isNull() and not avatar.avatar_path:
                previews.append((model_id, remote_avatar))
        if restore_row >= 0:
            self.list_widget.setCurrentRow(restore_row)
        self.status_label.setText(f"模型清单已更新 · 共 {len(self.models)} 个角色")
        if previews:
            self._load_previews(previews)
        self._update_buttons()

    def _catalog_failed(self, message: str) -> None:
        self.status_label.setText(f"模型清单暂时无法加载：{message}")

    def _catalog_finished(self) -> None:
        finished_thread = self.catalog_thread
        self.catalog_thread = None
        self.catalog_worker = None
        _ow_delete_finished_thread(finished_thread)
        self.refresh_button.setEnabled(True)
        self.back_button.setEnabled(self.action_thread is None)
        self._update_buttons()

    def _load_previews(self, previews: list[tuple[str, str]]) -> None:
        if self.preview_thread:
            return
        self.preview_thread = QThread(self)
        self.preview_worker = ModelPreviewWorker(previews)
        self.preview_worker.moveToThread(self.preview_thread)
        self.preview_thread.started.connect(self.preview_worker.run)
        self.preview_worker.loaded.connect(self._apply_preview)
        self.preview_worker.finished.connect(self.preview_thread.quit)
        self.preview_worker.finished.connect(self.preview_worker.deleteLater)
        self.preview_thread.finished.connect(self._preview_finished)
        # preview_thread 由页面生命周期统一回收，避免 finished 后留下失效引用。
        self.preview_thread.start()

    def _apply_preview(self, model_id: str, data: bytes) -> None:
        avatar = self.avatar_labels.get(model_id)
        if not avatar:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(QByteArray(data)):
            avatar.set_pixmap(pixmap)

    def _preview_finished(self) -> None:
        finished_thread = self.preview_thread
        self.preview_thread = None
        self.preview_worker = None
        _ow_delete_finished_thread(finished_thread)

    def _selected_model(self) -> dict | None:
        row = self.list_widget.currentRow()
        return self.models[row] if 0 <= row < len(self.models) else None

    def _update_buttons(self, *_args) -> None:
        model = self._selected_model()
        installed = bool(model and model.get("installed"))
        version = model.get("version") if model else None
        installed_version = model.get("installedVersion") if model else None
        needs_update = bool(installed and version and installed_version and str(version) != str(installed_version))
        self.install_button.setText("更新角色" if needs_update else "安装角色")
        self.install_button.setEnabled(bool(model and (not installed or needs_update) and not self.action_thread))
        self.rename_button.setEnabled(bool(installed and not self.action_thread))
        self.delete_button.setEnabled(bool(installed and not self.action_thread))

    def install_selected(self) -> None:
        model = self._selected_model()
        if model:
            installed = bool(model.get("installed"))
            version = model.get("version")
            installed_version = model.get("installedVersion")
            action = "update" if installed and version and installed_version and str(version) != str(installed_version) else "install"
            self._run_action(str(model.get("id")), action)

    def delete_selected(self) -> None:
        model = self._selected_model()
        if model and QMessageBox.question(self, "确认移除", f"确定移除“{model.get('display_name') or model.get('name') or model.get('id')}”吗？\n模型文件不会被删除。") == QMessageBox.Yes:
            self._run_action(str(model.get("id")), "delete")

    def rename_selected(self) -> None:
        model = self._selected_model()
        if not model or not model.get("installed") or self.action_thread:
            return
        current_name = str(model.get("display_name") or model.get("name") or model.get("id") or "")
        dialog = QDialog(self)
        dialog.setWindowTitle("重命名模型")
        dialog.setObjectName("ModelRenameDialog")
        dialog.setFixedWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        title = QLabel("重命名模型")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        current = QLabel(f"当前名称：{current_name}")
        current.setObjectName("ModelRenameCurrent")
        layout.addWidget(current)
        layout.addWidget(QLabel("新名称"))
        name_input = QLineEdit(current_name)
        name_input.setObjectName("ModelRenameInput")
        name_input.setFixedWidth(240)
        name_input.selectAll()
        layout.addWidget(name_input, 0, Qt.AlignLeft)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("ModelCloseButton")
        cancel.clicked.connect(dialog.reject)
        confirm = QPushButton("确定")
        confirm.setObjectName("ModelRenameButton")
        confirm.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        new_name = name_input.text().strip()
        if not new_name or new_name == current_name:
            return
        self._run_action(str(model.get("id")), "rename", new_name=new_name)

    def _run_action(self, model_id: str, action: str, source_dir: str | None = None, new_name: str | None = None) -> None:
        self.action_thread = QThread(self)
        self.action_worker = ModelActionWorker(self.api_url, model_id, action, source_dir, new_name)
        self.action_worker.moveToThread(self.action_thread)
        self.action_thread.started.connect(self.action_worker.run)
        self.action_worker.success.connect(self._action_success)
        self.action_worker.failed.connect(self._action_failed)
        self.action_worker.finished.connect(self.action_thread.quit)
        self.action_worker.finished.connect(self.action_worker.deleteLater)
        self.action_thread.finished.connect(self._action_finished)
        # action_thread 由页面生命周期统一回收，避免 finished 后留下失效引用。
        self.install_button.setEnabled(False)
        self.rename_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.status_label.setText("正在处理模型，请耐心等待……")
        self.action_thread.start()

    def _action_success(self, message: str) -> None:
        self.status_label.setText(message)
        self.models_changed.emit()
        QTimer.singleShot(50, self.refresh_catalog)

    def _action_failed(self, message: str) -> None:
        self.status_label.setText(f"模型操作失败：{message}")
        QMessageBox.warning(self, "模型操作失败", message)

    def _action_finished(self) -> None:
        finished_thread = self.action_thread
        self.action_thread = None
        self.action_worker = None
        _ow_delete_finished_thread(finished_thread)
        self.back_button.setEnabled(self.catalog_thread is None)
        self._update_buttons()

class OwVoiceApp(QMainWindow):
    def __init__(self, startup_mode: bool = False) -> None:
        super().__init__()
        self.startup_mode = startup_mode
        self.setWindowTitle("OwVoice - 守望先锋角色语音工具")
        if ICON_PATH.is_file():
            icon = QIcon(str(ICON_PATH))
            self.setWindowIcon(icon)
            application = QApplication.instance()
            if application is not None:
                application.setWindowIcon(icon)
        self.resize(1040, 720)
        self.setMinimumSize(860, 620)
        self.synthesis_thread: QThread | None = None
        self.worker: SynthesisWorker | None = None
        self.model_release_thread: QThread | None = None
        self.model_release_worker: ModelEngineUnloadWorker | None = None
        self.engine_start_thread: QThread | None = None
        self.engine_start_worker: EngineStartWorker | None = None
        self.dynamic_engine_pid = 0
        self._last_health: dict = {}
        self.output_dir = OUTPUT_DIR
        self.last_audio = self.output_dir / "last.wav"
        self.last_audio_by_voice: dict[str, Path] = {}
        self.prompt_by_voice: dict[str, str] = {}
        self._loaded_default_prompts: dict[str, str] = {}
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
        self.media_devices = QMediaDevices(self)
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.media_devices.audioOutputsChanged.connect(self.refresh_audio_output)
        self.refresh_audio_output()
        self.playback_buffer: QBuffer | None = None
        self.playback_bytes = b""
        self.play_when_loaded = False
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("输入需要合成的角色台词……")
        self.text_input.setPlainText("任务开始了。")
        self.status_label = QLabel("正在连接 OwVoice 后端……")
        self.status_label.setObjectName("StatusLabel")
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
        self.model_plaza_button = QPushButton("模型广场")
        self.model_plaza_button.setObjectName("ModelPlazaButton")
        self.model_plaza_button.clicked.connect(self.open_model_plaza)
        self.about_button = QPushButton("关于 / 更新")
        self.about_button.setObjectName("AboutButton")
        self.about_button.clicked.connect(self.check_updates)

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
            QLabel { background-color: transparent; font-size: 14px; }
            QFrame#Sidebar { background-color: #E8E6DC; border: 1px solid #D8D5CA; border-radius: 14px; }
            QFrame#SectionCard, QFrame#TextCard { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 10px; }
            QFrame#BrandCard { background-color: transparent; border: none; border-bottom: 1px solid #D1CFC5; border-radius: 0px; }
            QLabel#BrandMark { color: #CC785C; font-family: '{self.mono_font}', 'Microsoft YaHei', sans-serif; font-size: 13px; font-weight: bold; }
            QLabel#BrandTitle { color: #141413; font-family: '{self.display_font}', 'Microsoft YaHei', sans-serif; font-size: 34px; font-weight: 600; }
            QLabel#BrandSubtitle { color: #5E5D59; font-size: 14px; }
            QLabel#Hint, QLabel#FieldHint { color: #5E5D59; font-size: 13px; }
            QLabel#PageKicker { color: #CC785C; font-family: '{self.mono_font}', 'Microsoft YaHei', sans-serif; font-size: 11px; font-weight: bold; }
            QLabel#PageTitle { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 32px; font-weight: 600; }
            QLabel#VersionLabel { color: #5DB872; font-size: 16px; font-weight: 700; padding: 0 0 5px 8px; }
            QLabel#CurrentVoice { color: #141413; font-family: '{self.ui_font}', 'Microsoft YaHei', sans-serif; font-size: 23px; font-weight: 600; }
            QLabel#VoiceName { color: #141413; background-color: transparent; font-size: 18px; font-weight: 600; }
            QLabel#SectionTitle { color: #141413; font-size: 15px; font-weight: 600; }
            QLabel#StatusLabel { color: #5DB872; font-size: 17px; font-weight: 700; padding: 5px 0; }
            QLineEdit, QTextEdit { background-color: #FFFFFF; border: 1px solid #E8E6DC; border-radius: 8px; padding: 9px 11px; color: #141413; font-size: 15px; }
            QLineEdit:focus, QTextEdit:focus { border: 1px solid #CC785C; }
            QTextEdit { padding: 13px; }
            QPushButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 8px; padding: 12px 20px; font-weight: 600; font-size: 16px; }
            QPushButton:hover { background-color: #B95738; }
            QPushButton:pressed { background-color: #A84A30; }
            QPushButton:disabled { background-color: rgba(20, 20, 19, 0.16); color: rgba(20, 20, 19, 0.42); }
            QPushButton#SecondaryButton { background-color: #E8E6DC; color: #4D4C48; border: 1px solid #D1CFC5; }
            QPushButton#SecondaryButton:hover { background-color: #DCD9CF; border-color: #BDBAB0; }
             QPushButton:focus { outline: none; }
             QPushButton#ModelPlazaButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 10px; padding: 11px 16px; font-size: 17px; font-weight: 700; }
             QPushButton#ModelPlazaButton:hover { background-color: #B95738; }
             QPushButton#ModelPlazaButton:pressed { background-color: #A84A30; }
             QPushButton#TrainingButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 10px; padding: 11px 16px; font-size: 17px; font-weight: 700; }
             QPushButton#TrainingButton:hover { background-color: #B95738; }
             QPushButton#TrainingButton:pressed { background-color: #A84A30; }
             QPushButton#AboutButton { background-color: transparent; color: #5E5D59; border: 1px solid transparent; border-radius: 8px; padding: 9px 14px; font-size: 15px; font-weight: 600; }
             QPushButton#AboutButton:hover { background-color: #F0EEE6; color: #141413; border-color: #D8D5CA; }
             QDialog#ModelPlazaDialog { background-color: #F5F4ED; }
             QWidget#ModelRow { background-color: transparent; }
             QWidget#ModelPlazaPage { background-color: transparent; }
             QPushButton#ModelBackButton, QPushButton#TrainingBackButton { background-color: transparent; color: #5E5D59; border: 1px solid #D1CFC5; border-radius: 10px; padding: 12px 20px; font-size: 18px; font-weight: 700; }
             QPushButton#ModelBackButton:hover, QPushButton#TrainingBackButton:hover { background-color: #E8E6DC; color: #141413; border-color: #BDBAB0; }
             QPushButton#ModelRefreshButton { background-color: transparent; color: #5E5D59; border: 1px solid #D1CFC5; border-radius: 10px; padding: 12px 18px; font-size: 16px; font-weight: 600; }
             QPushButton#ModelRefreshButton:hover { background-color: #E8E6DC; color: #141413; border-color: #BDBAB0; }
             QLabel#ModelPlazaPageTitle { color: #141413; font-size: 34px; font-weight: 700; }
             QLabel#ModelPlazaPageSubtitle { color: #5E5D59; font-size: 16px; }
             QLabel#ModelPlazaPageStatus { color: #5DB872; font-size: 17px; font-weight: 700; padding: 5px 0; }
             QListWidget#ModelPlazaPageList { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 14px; padding: 9px; outline: none; }
             QListWidget#ModelPlazaPageList::item { background-color: #F5F4ED; border: 1px solid #E8E6DC; border-radius: 11px; margin: 3px 1px; }
             QListWidget#ModelPlazaPageList::item:hover { background-color: #F0EEE6; border-color: #D8D5CA; }
             QListWidget#ModelPlazaPageList::item:selected { background-color: #FFF5F0; border: 1px solid #CC785C; }
             QWidget#ModelCard { background-color: transparent; }
             QLabel#ModelCardTitle { color: #141413; font-size: 20px; font-weight: 700; }
             QLabel#ModelCardMeta { color: #8A8881; font-size: 15px; }
             QLabel#ModelCardState, QLabel#ModelCardStateInstalled, QLabel#ModelCardStateUpdate { border-radius: 13px; padding: 7px 13px; font-size: 15px; font-weight: 700; }
             QLabel#ModelCardState { color: #CC785C; background-color: #FBE9E1; }
             QLabel#ModelCardStateInstalled { color: #4C9A5D; background-color: #E5F3E7; }
             QLabel#ModelCardStateUpdate { color: #A87927; background-color: #FFF0C9; }
             QLabel#DialogTitle { color: #141413; font-size: 28px; font-weight: 700; }
             QLabel#ModelPlazaIntro, QLabel#ModelPlazaStatus { color: #5E5D59; font-size: 15px; }
             QLabel#ModelPlazaStatus { color: #5DB872; font-weight: 600; }
             QListWidget#ModelPlazaList { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 12px; padding: 6px; outline: none; }
             QListWidget#ModelPlazaList::item { background-color: #F5F4ED; border: 1px solid #E8E6DC; border-radius: 9px; margin: 3px 1px; }
             QListWidget#ModelPlazaList::item:hover { background-color: #F0EEE6; border-color: #D8D5CA; }
             QListWidget#ModelPlazaList::item:selected { background-color: #FFF5F0; border: 1px solid #CC785C; }
             QLabel#ModelRowTitle { color: #141413; font-size: 15px; font-weight: 700; }
             QLabel#ModelRowMeta { color: #8A8881; font-size: 12px; }
             QLabel#ModelRowState, QLabel#ModelRowStateInstalled { color: #CC785C; background-color: #FBE9E1; border-radius: 10px; padding: 4px 9px; font-size: 12px; font-weight: 600; }
             QLabel#ModelRowStateInstalled { color: #4C9A5D; background-color: #E5F3E7; }
             QPushButton#CatalogRefreshButton, QPushButton#ModelDeleteButton, QPushButton#ModelRenameButton, QPushButton#ModelCloseButton { background-color: transparent; color: #5E5D59; border: 1px solid #D1CFC5; border-radius: 10px; padding: 11px 16px; font-size: 16px; }
             QPushButton#CatalogRefreshButton:hover, QPushButton#ModelDeleteButton:hover, QPushButton#ModelRenameButton:hover, QPushButton#ModelCloseButton:hover { background-color: #E8E6DC; color: #141413; }
             QDialog#ModelRenameDialog { background-color: #F5F4ED; }
             QLabel#ModelRenameCurrent { color: #5E5D59; font-size: 15px; }
             QLineEdit#ModelRenameInput { background-color: #FAF9F5; color: #141413; border: 1px solid #D1CFC5; border-radius: 8px; padding: 8px 10px; font-size: 16px; }
             QLineEdit#ModelRenameInput:focus { border-color: #CC785C; }
             QPushButton#ModelInstallButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 10px; padding: 12px 20px; font-size: 17px; font-weight: 700; }
             QPushButton#ModelInstallButton:hover { background-color: #B95738; }
            QPushButton#VoiceCard { background-color: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0; font-size: 15px; }
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
             QWidget#TrainingPage { background-color: transparent; }
             QScrollArea#TrainingScroll, QWidget#TrainingContent { background-color: transparent; border: none; }
             QFrame#TrainingCard { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 12px; }
             QFrame#TrainingActionCard { background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 12px; }
             QLabel#TrainingPageTitle { color: #141413; font-size: 34px; font-weight: 700; }
             QLabel#TrainingPageSubtitle, QLabel#TrainingHint { color: #5E5D59; font-size: 14px; }
             QLabel#TrainingStepLabel { color: #CC785C; font-size: 15px; font-weight: 700; padding: 4px 0; }
             QLabel#TrainingStepItem { background-color: #E8E6DC; color: #8A8881; border-radius: 8px; padding: 6px 8px; font-size: 13px; font-weight: 600; }
             QLabel#TrainingStepItem[active="true"] { background-color: #FBE9E1; color: #B95738; }
             QPushButton#TrainingStepItem { background-color: #E8E6DC; color: #8A8881; border: none; border-radius: 8px; padding: 6px 8px; font-size: 13px; font-weight: 600; }
             QPushButton#TrainingStepItem:hover { background-color: #DDD9CC; color: #5E5D59; }
             QPushButton#TrainingStepItem[active="true"] { background-color: #FBE9E1; color: #B95738; }
             QLabel#TrainingGlobalStatus { background-color: #F0EEE6; color: #5E5D59; border: 1px solid #E8E6DC; border-radius: 8px; padding: 9px 12px; font-size: 14px; font-weight: 600; }
             QLabel#TrainingSectionTitle { color: #141413; font-size: 18px; font-weight: 700; }
             QLabel#TrainingFieldLabel { color: #141413; font-size: 16px; font-weight: 700; }
             QLabel#TrainingStatusLabel { color: #5DB872; font-size: 15px; font-weight: 700; }
             QLabel#TrainingStatusLabel[error="true"] { color: #B5443C; }
             QLabel#TrainingGlobalStatus[error="true"] { color: #B5443C; background-color: #FCEDEA; border-color: #E8B7AE; }
             QLabel#TrainingFixedLanguage { background-color: #F0EEE6; border: 1px solid #E8E6DC; border-radius: 8px; padding: 8px 10px; color: #5E5D59; font-size: 15px; }
             QLineEdit#TrainingNameInput, QComboBox { background-color: #FFFFFF; border: 1px solid #D8D5CA; border-radius: 8px; padding: 8px 10px; color: #141413; font-size: 15px; min-height: 30px; }
             QTableWidget#TrainingTranscriptTable { background-color: #FFFFFF; alternate-background-color: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 8px; gridline-color: #F0EEE6; color: #141413; font-size: 15px; outline: none; }
             QTableWidget#TrainingTranscriptTable QHeaderView::section { background-color: #F0EEE6; color: #141413; border: none; padding: 10px; font-size: 16px; font-weight: 700; }
             QListWidget#TrainingFileList { background-color: #FFFFFF; border: 1px solid #E8E6DC; border-radius: 10px; padding: 6px; color: #141413; font-size: 15px; outline: none; }
             QListWidget#TrainingFileList::item { background-color: #FAF9F5; border: 1px solid #EEECE4; padding: 10px 12px; margin: 2px; border-radius: 8px; min-height: 28px; }
             QListWidget#TrainingFileList::item:hover { background-color: #FAF3EE; }
             QListWidget#TrainingFileList::item:selected { background-color: #FBE9E1; color: #141413; border: 1px solid transparent; }
             QProgressBar#TrainingProgress { background-color: #E8E6DC; border: none; border-radius: 5px; }
             QProgressBar#TrainingProgress::chunk { background-color: #C96442; border-radius: 5px; }
             QTextEdit#TrainingLog { background-color: #2B2926; color: #F5F4ED; border: none; border-radius: 8px; padding: 8px; font-family: 'Consolas'; font-size: 12px; }
             QPushButton#TrainingSecondaryButton { background-color: transparent; color: #5E5D59; border: 1px solid #D1CFC5; border-radius: 9px; padding: 10px 15px; font-size: 15px; font-weight: 600; }
             QPushButton#TrainingSecondaryButton:hover { background-color: #E8E6DC; color: #141413; }
             QPushButton#TrainingPrimaryButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 9px; padding: 10px 16px; font-size: 15px; font-weight: 700; }
             QPushButton#TrainingPrimaryButton:hover { background-color: #B95738; }
             QPushButton#TrainingPrimaryButton:disabled { background-color: #D8D5CA; color: #8A8881; }
             QPushButton#TrainingViewButton { background-color: transparent; color: #5E5D59; border: 1px solid #D1CFC5; border-radius: 8px; padding: 7px 12px; font-size: 13px; font-weight: 600; }
             QPushButton#TrainingViewButton:hover { background-color: #F0EEE6; color: #141413; }
             QPushButton#TrainingViewButton:checked { background-color: #FBE9E1; color: #B95738; border-color: #CC785C; }
             QPushButton#TrainingPreviewButton { background-color: #F0EEE6; color: #5E5D59; border: 1px solid #D8D5CA; border-radius: 7px; padding: 4px; }
             QPushButton#TrainingPreviewButton:hover { background-color: #FBE9E1; color: #B95738; border-color: #CC785C; }
             QDialog#TrainingLogDialog { background-color: #F5F4ED; }
             QLabel#TrainingLogDialogTitle { color: #141413; font-size: 22px; font-weight: 700; }
             QTextEdit#TrainingLogDialogText, QPlainTextEdit#TrainingLogDialogText { background-color: #242321; color: #F5F4ED; border: none; border-radius: 9px; padding: 10px; font-family: 'Consolas'; font-size: 13px; }
             QLabel#SpeedLabel { color: #141413; font-size: 15px; font-weight: 600; }
            QDoubleSpinBox#SpeedInput { background-color: #FFFFFF; border: 1px solid #D8D5CA; border-radius: 8px; padding: 4px 8px; color: #141413; font-size: 19px; font-weight: 600; min-height: 34px; }
            QDoubleSpinBox#SpeedInput:focus { border: 1px solid #CC785C; }
            """
            .replace("{self.ui_font}", self.ui_font)
            .replace("{self.display_font}", self.display_font)
            .replace("{self.mono_font}", self.mono_font)
        )

        root_layout.addWidget(self.build_sidebar())
        self.workspace_stack = QStackedWidget()
        self.workspace_page = self.build_workspace()
        self.model_plaza_page = ModelPlazaPage(self.api_url(), self)
        self.model_plaza_page.models_changed.connect(self._on_models_changed)
        self.model_plaza_page.back_requested.connect(self.show_workspace)
        self.workspace_stack.addWidget(self.workspace_page)
        self.workspace_stack.addWidget(self.model_plaza_page)
        root_layout.addWidget(self.workspace_stack, 1)
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
        # 后台检查，不阻塞首次启动；同一版本只提示一次。
        QTimer.singleShot(1800, self.check_updates_silently)

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

        self.model_plaza_button.setFixedHeight(46)
        layout.addWidget(self.model_plaza_button)
        self.about_button.setFixedHeight(40)
        layout.addWidget(self.about_button)

        for button in sidebar.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)

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
        title_row = QHBoxLayout()
        page_title = QLabel("配音工作台")
        page_title.setObjectName("PageTitle")
        version_label = QLabel(f"OwVoice v{APP_VERSION}")
        version_label.setObjectName("VersionLabel")
        title_row.addWidget(page_title)
        title_row.addWidget(version_label, 0, Qt.AlignBottom)
        title_row.addStretch()
        title_box.addLayout(title_row)
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
        for button in workspace.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        return workspace

    def check_updates_silently(self) -> None:
        self.check_updates(silent=True)

    def _update_state_path(self) -> Path:
        return PROJECT_DIR / ".cache" / "update-check.json"

    def _was_update_notified(self, version: str) -> bool:
        try:
            state = json.loads(self._update_state_path().read_text(encoding="utf-8"))
            return str(state.get("notifiedVersion", "")) == version
        except (OSError, json.JSONDecodeError):
            return False

    def _remember_update_notification(self, version: str) -> None:
        path = self._update_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"notifiedVersion": version}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def check_updates(self, silent: bool = False) -> None:
        self.about_button.setEnabled(False)
        try:
            response = requests.get(f"{self.api_url()}/api/updates", timeout=30)
            response.raise_for_status()
            update = response.json()
            app_update = update.get("app") or {}
            if not app_update.get("available"):
                if not silent:
                    QMessageBox.information(self, "检查更新", f"当前已是最新版本（{update.get('currentVersion', APP_VERSION)}）。")
                return
            latest_version = str(update.get("latestVersion", ""))
            if silent and self._was_update_notified(latest_version):
                return
            download_url = str(app_update.get("downloadUrl") or "")
            sha256 = str(app_update.get("sha256") or "")
            script = PROJECT_DIR / "scripts" / "update_release.ps1"
            if not download_url or len(sha256) != 64 or not script.is_file():
                if not silent:
                    QMessageBox.warning(self, "暂时无法更新", "发现新版本，但当前发布包缺少完整校验信息，请稍后再试。")
                return
            if silent:
                self._remember_update_notification(latest_version)
            answer = QMessageBox.question(
                self,
                "发现新版本",
                f"发现 OwVoice {latest_version}，是否现在下载并更新？\n\n更新会保留你的配置、角色模型、输出和缓存。",
            )
            if answer != QMessageBox.Yes:
                return
            command = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
                "-DownloadUrl", download_url,
                "-Sha256", sha256,
                "-TargetDirectory", str(PROJECT_DIR),
                "-WaitPid", str(os.getpid()),
                "-RestartPath", str(PROJECT_DIR / "OwVoice.exe"),
            ]
            subprocess.Popen(command, cwd=str(PROJECT_DIR), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.set_status("更新程序已启动，正在退出并替换文件……", warning=True)
            QTimer.singleShot(300, QApplication.quit)
        except Exception as exc:  # noqa: BLE001 - 显示更新检查错误
            if not silent:
                QMessageBox.warning(self, "检查更新失败", str(exc))
        finally:
            self.about_button.setEnabled(True)
    def show_workspace(self) -> None:
        self.workspace_stack.setCurrentWidget(self.workspace_page)

    def open_model_plaza(self) -> None:
        self.workspace_stack.setCurrentWidget(self.model_plaza_page)
        self.model_plaza_page.refresh_catalog()

    def load_voices(self) -> None:
        try:
            api_url = self.api_url()
            health_response = requests.get(f"{api_url}/api/health", timeout=3)
            health_response.raise_for_status()
            health = health_response.json()
            self._last_health = health if isinstance(health, dict) else {}
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

        changed_default_ids = _sync_voice_prompt_defaults(
            self._loaded_default_prompts,
            self.prompt_by_voice,
            voices,
        )
        self.voices = voices
        for card in self.voice_cards.values():
            card.deleteLater()
        self.voice_cards.clear()
        while self.role_layout.count() > 1:
            item = self.role_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
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
            self.clear_current_voice()
            self.set_status("没有可用角色", error=True)
            return
        self.generate_button.setEnabled(bool(health.get("gpt_sovits_online")))
        selected_voice_id = health.get("selected_voice_id")
        available_ids = {str(voice.get("id")) for voice in self.voices}
        target_voice_id = selected_voice_id if selected_voice_id in available_ids else self.voices[0]["id"]
        self.select_voice(target_voice_id)
        if target_voice_id in changed_default_ids:
            current_voice = next(
                (voice for voice in self.voices if voice.get("id") == target_voice_id),
                None,
            )
            if current_voice is not None:
                self.text_input.setPlainText(self.default_prompt(current_voice))

    def _on_models_changed(self) -> None:
        """模型清单变化后刷新角色，并在空工作台中启动推理引擎。"""
        self.load_voices()
        if not self.voices or self._last_health.get("gpt_sovits_online"):
            return
        selected_id = self._last_health.get("selected_voice_id")
        voice = next((item for item in self.voices if item.get("id") == selected_id), None)
        self.start_dynamic_engine(voice or self.voices[0])

    def start_dynamic_engine(self, voice: dict) -> None:
        if self.engine_start_thread is not None:
            return
        self.generate_button.setEnabled(False)
        self.set_status("正在启动语音引擎……", warning=True)
        self.engine_start_thread = QThread(self)
        self.engine_start_worker = EngineStartWorker(PROJECT_DIR, voice)
        self.engine_start_worker.moveToThread(self.engine_start_thread)
        self.engine_start_thread.started.connect(self.engine_start_worker.run)
        self.engine_start_worker.ready.connect(self._dynamic_engine_ready)
        self.engine_start_worker.failed.connect(self._dynamic_engine_failed)
        self.engine_start_worker.finished.connect(self.engine_start_thread.quit)
        self.engine_start_worker.finished.connect(self.engine_start_worker.deleteLater)
        self.engine_start_thread.finished.connect(self._dynamic_engine_finished)
        # engine_start_thread 由主窗口关闭流程统一回收，避免异步回调访问失效对象。
        self.engine_start_thread.start()

    def stop_inference_service_for_training(self) -> None:
        """训练前释放推理引擎，避免与本地训练争抢显存。"""

        # 启动阶段创建的 GPT-SoVITS 进程由 StartupWorker 持有；动态启动的
        # 进程则由当前窗口记录。两种来源都要处理，避免训练时残留推理进程。
        controller = getattr(self, "startup_controller", None)
        startup_worker = getattr(controller, "worker", None)
        if startup_worker is not None and hasattr(startup_worker, "stop_service"):
            startup_worker.stop_service("gpt_sovits")
        if self.dynamic_engine_pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.dynamic_engine_pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except OSError:
                pass
            self.dynamic_engine_pid = 0
        self._last_health["gpt_sovits_online"] = False

    @Slot(int)
    def _dynamic_engine_ready(self, process_id: int) -> None:
        self.dynamic_engine_pid = process_id
        self.set_status("语音引擎已启动，正在刷新角色……")
        self.load_voices()

    @Slot(str)
    def _dynamic_engine_failed(self, message: str) -> None:
        self.set_status(f"语音引擎启动失败：{message}", error=True)
        self.generate_button.setEnabled(False)
        QMessageBox.warning(self, "语音引擎启动失败", message)

    @Slot()
    def _dynamic_engine_finished(self) -> None:
        finished_thread = self.engine_start_thread
        self.engine_start_thread = None
        self.engine_start_worker = None
        _ow_delete_finished_thread(finished_thread)

    def clear_current_voice(self) -> None:
        """清空已删除角色在界面上的残留状态。"""
        self.current_voice_id = None
        self.current_voice_label.setText("未选择")
        self.text_input.clear()
        self.filename_input.clear()
        self.prompt_by_voice.clear()
        self.last_audio_by_voice.clear()
        self.last_audio = Path()
        self.pending_audio = None
        self.pending_voice_id = None
        self.release_player_source()
        self.play_button.setEnabled(False)
        self.generate_button.setEnabled(False)

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
        if voice_changed:
            try:
                response = requests.post(
                    f"{self.api_url()}/api/voices/{voice_id}/select",
                    timeout=3,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                self.set_status(f"角色切换同步失败：{exc}", error=True)
                return
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
        return _voice_default_prompt(voice)

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
        self.synthesis_thread = QThread(self)
        request_id = uuid.uuid4().hex
        self.worker = SynthesisWorker(
            self.api_url(),
            voice_id,
            text,
            self.speed_input.value(),
            request_id,
        )
        self.worker.moveToThread(self.synthesis_thread)
        self.synthesis_thread.started.connect(self.worker.run)
        self.worker.success.connect(self.save_audio)
        # 直接连接到 QMainWindow 的槽，确保错误对话框在 GUI 线程创建；
        # 无上下文 lambda 可能在工作线程中操作 QWidget，触发 Qt 跨线程警告。
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.synthesis_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.synthesis_thread.finished.connect(self._synthesis_finished)
        self.synthesis_thread.start()

    @Slot(bytes)
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

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_status("合成失败", error=True)
        QMessageBox.critical(self, "合成失败", message)

    def _synthesis_finished(self) -> None:
        finished_thread = self.synthesis_thread
        self.synthesis_thread = None
        self.worker = None
        _ow_delete_finished_thread(finished_thread)
        self.set_generation_controls_enabled(True)

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

    def refresh_audio_output(self) -> None:
        """将播放器绑定到系统当前默认音频输出（耳机/扬声器）。"""
        default_device = QMediaDevices.defaultAudioOutput()
        if default_device.isNull():
            return

        if self.audio_output.device() != default_device:
            self.audio_output.setDevice(default_device)

    def _start_playback(self) -> None:
        if not self.play_when_loaded:
            return
        self.refresh_audio_output()
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
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 17px; font-weight: bold; padding: 4px 0;"
        )

    def set_generation_controls_enabled(self, enabled: bool) -> None:
        self.generate_button.setEnabled(enabled and bool(self.voices))
        self.speed_slider.setEnabled(enabled)
        self.speed_input.setEnabled(enabled)
        self.output_dir_input.setEnabled(enabled)
        self.filename_input.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)
        self.open_file_button.setEnabled(enabled)

    def closeEvent(self, event) -> None:
        training_page = getattr(self, "training_page", None)
        if training_page is not None and not training_page._ow_training_can_close():
            event.ignore()
            return
        self.release_player_source()
        # 先停掉所有由窗口创建的定时器，避免窗口开始销毁后仍有槽函数访问控件。
        for page in (self, getattr(self, "training_page", None), getattr(self, "model_plaza_page", None)):
            if page is None:
                continue
            for timer_name in ("poll_timer", "training_log_refresh_timer", "status_animation_timer", "training_heartbeat_timer"):
                timer = getattr(page, timer_name, None)
                if timer is not None:
                    timer.stop()
        preview_player = getattr(training_page, "preview_player", None)
        if preview_player is not None:
            preview_player.stop()

        # Qt 不允许销毁仍在运行的 QThread。关闭时等待短时间让正常收尾完成；
        # 网络请求或环境安装若仍未结束，则保留窗口，避免 native crash。
        background_threads: list[QThread] = []
        for owner in (
            self,
            training_page,
            getattr(self, "model_plaza_page", None),
            getattr(self, "startup_controller", None),
        ):
            if owner is None:
                continue
            for name in (
                "synthesis_thread",
                "startup_thread",
                "model_release_thread",
                "engine_start_thread",
                "request_thread",
                "training_setup_thread",
                "avatar_preview_thread",
                "catalog_thread",
                "preview_thread",
                "action_thread",
            ):
                thread = getattr(owner, name, None)
                if thread is not None and thread not in background_threads:
                    background_threads.append(thread)
        still_running = []
        for thread in background_threads:
            # QThread 可能已经在 finished 信号中由 deleteLater 删除；
            # 关闭窗口收尾时不能再调用其 C++ 方法，否则会触发 RuntimeError，
            # 在 Windows 上表现为窗口卡退。
            try:
                running = thread.isRunning()
            except RuntimeError:
                continue
            if not running:
                continue
            try:
                thread.requestInterruption()
                thread.quit()
                finished = thread.wait(1500)
            except RuntimeError:
                # 线程在收尾期间被 Qt 删除，视为已经结束。
                continue
            if not finished:
                still_running.append(thread)
        if still_running:
            QMessageBox.warning(
                self,
                "后台任务仍在运行",
                "当前有后台操作尚未结束，程序暂不关闭。\n请稍后再点击关闭；本地训练后端会继续运行。",
            )
            event.ignore()
            return

        if self.dynamic_engine_pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.dynamic_engine_pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except OSError:
                pass
            self.dynamic_engine_pid = 0
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

# 公开版兼容层：本地模型库、本地导入和本地训练入口。
_ow_model_page_base_init = ModelPlazaPage.__init__


def _ow_import_local_model(self) -> None:
    default_dir = PROJECT_DIR / "data" / "models"
    if not default_dir.is_dir():
        default_dir = PROJECT_DIR
    source_dir = QFileDialog.getExistingDirectory(self, "选择单个模型包或模型集合文件夹", str(default_dir))
    if not source_dir:
        return
    self._run_action("", "import", source_dir=source_dir)

def _ow_update_local_model_buttons(self, *_args) -> None:
    model = self._selected_model()
    installed = bool(model and model.get("installed"))
    self.install_button.setText("导入模型")
    idle = self.action_thread is None and self.catalog_thread is None
    self.install_button.setEnabled(idle)
    self.rename_button.setEnabled(bool(installed and idle))
    self.delete_button.setEnabled(bool(installed and idle))


def _ow_model_page_init(self, api_url: str, parent=None):
    _ow_model_page_base_init(self, api_url, parent)
    self._ow_local_mode = True
    self.setWindowTitle("本地模型库")
    self.refresh_button.setText("刷新")
    self.install_button.setText("导入模型")
    try:
        self.install_button.clicked.disconnect()
    except (RuntimeError, TypeError):
        pass
    self.install_button.clicked.connect(lambda: _ow_import_local_model(self))
    for label in self.findChildren(QLabel):
        if label.objectName() == "ModelPlazaPageTitle":
            label.setText("本地模型库")
        if label.objectName() == "ModelPlazaPageSubtitle":
            label.setText("导入或管理本地模型。请确认音频、模型及训练数据具有合法授权。")
    for button in (self.back_button, self.refresh_button, self.install_button, self.rename_button, self.delete_button):
        button.setMinimumHeight(50)
    self._update_buttons()


ModelPlazaPage._update_buttons = _ow_update_local_model_buttons
ModelPlazaPage.__init__ = _ow_model_page_init


def _ow_write_frontend_error(context: str, exc: BaseException) -> Path:
    """没有控制台时，把 Qt 主线程异常写入项目日志。"""
    log_path = PROJECT_DIR / "logs" / "frontend.error.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n{detail}")
    except OSError:
        pass
    return log_path


def _ow_training_label(text: str, object_name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    return label


def _ow_training_card() -> QFrame:
    card = QFrame()
    card.setObjectName("TrainingCard")
    return card


def _ow_training_hint(text: str) -> QLabel:
    label = _ow_training_label(text, "TrainingHint")
    label.setWordWrap(True)
    return label


class _TrainingFileList(QListWidget):
    """训练素材列表：普通点击即可逐项切换选中状态，支持多选删除。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view_kind = "columns"
        self.setTextElideMode(Qt.ElideRight)
        self.setWordWrap(False)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)

    def set_view_kind(self, kind: str) -> None:
        self.view_kind = "tiles" if kind == "tiles" else "columns"
        if self.view_kind == "tiles":
            self.setViewMode(QListView.ListMode)
            self.setFlow(QListView.TopToBottom)
            self.setWrapping(False)
            self.setGridSize(QSize())
            for index in range(self.count()):
                self.item(index).setSizeHint(QSize(0, 46))
        else:
            self.setViewMode(QListView.IconMode)
            self.setFlow(QListView.LeftToRight)
            self.setWrapping(True)
            self._update_column_size()
        self.doItemsLayout()

    def _update_column_size(self) -> None:
        if self.view_kind != "columns":
            return
        width = max(210, (self.viewport().width() - 34) // 2)
        self.setGridSize(QSize(width, 54))
        for index in range(self.count()):
            self.item(index).setSizeHint(QSize(width - 8, 46))

    def refresh_layout(self) -> None:
        if self.view_kind == "columns":
            self._update_column_size()
        self.doItemsLayout()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        super().resizeEvent(event)
        self._update_column_size()
        self.scheduleDelayedItemsLayout()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                item.setSelected(not item.isSelected())
                event.accept()
                return
            self.clearSelection()
        super().mousePressEvent(event)


class _ElidingLabel(QLabel):
    """始终保留开头，宽度不足时在右侧显示省略号。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        self._update_elision()

    def _update_elision(self) -> None:
        available = max(20, self.width() - 4)
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.ElideRight, available))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 重载方法名
        super().resizeEvent(event)
        self._update_elision()


def _ow_training_build_ui(self) -> None:
    """本地训练使用独立三阶段工作区，不复用首页或模型库的布局。"""
    self.setObjectName("TrainingPage")
    self.preview_player = QMediaPlayer(self)
    self.preview_audio_output = QAudioOutput(self)
    self.preview_audio_output.setVolume(1.0)
    self.preview_player.setAudioOutput(self.preview_audio_output)
    self.preview_player.mediaStatusChanged.connect(self._ow_training_preview_status)
    self.preview_player.errorOccurred.connect(self._ow_training_preview_error)
    self.preview_name = ""
    self.avatar_path = ""
    self._training_file_names: list[str] = []
    self._next_action = "transcribe"
    self._can_train = False
    self._avatar_available = False
    self.avatar_preview_thread: QThread | None = None
    self.avatar_preview_worker: ModelPreviewWorker | None = None
    self._status_base_text = ""
    self._status_dot_count = 0
    self.training_log_dialog = None
    self.training_log_text_edit = None
    self._training_log_cache = ""
    self._pending_training_navigation: str | None = None
    self.training_heartbeat_timer = QTimer(self)
    self.training_heartbeat_timer.setInterval(5000)
    self.training_heartbeat_timer.timeout.connect(self._ow_training_send_heartbeat)
    self.training_log_refresh_timer = QTimer(self)
    # 日志窗口不需要每秒重排整段文本，降低刷新频率可避免训练时阻塞界面。
    self.training_log_refresh_timer.setInterval(5000)
    self.training_log_refresh_timer.timeout.connect(self._ow_training_refresh_log)
    self.status_animation_timer = QTimer(self)
    self.status_animation_timer.setInterval(420)
    self.status_animation_timer.timeout.connect(self._ow_training_animate_status)
    page_layout = QVBoxLayout(self)
    page_layout.setContentsMargins(24, 12, 24, 12)
    page_layout.setSpacing(0)
    content = QWidget()
    content.setObjectName("TrainingContent")
    content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    page_layout.addWidget(content, 1)

    header = QHBoxLayout()
    self.back_button = QPushButton("返回")
    self.back_button.setObjectName("TrainingBackButton")
    self.back_button.clicked.connect(self._go_back)
    header.addWidget(self.back_button, 0, Qt.AlignTop)
    title_box = QVBoxLayout()
    title_box.setSpacing(4)
    title_box.addWidget(_ow_training_label("训练一个新声音", "TrainingPageTitle"))
    title_box.addWidget(_ow_training_hint("准备素材、确认文字，OwVoice 会自动完成数据准备和模型训练。"))
    header.addLayout(title_box, 1)
    layout.addLayout(header)

    self.step_label = _ow_training_label("当前步骤：准备训练素材", "TrainingStepLabel")
    layout.addWidget(self.step_label)
    step_strip = QHBoxLayout()
    step_strip.setSpacing(8)
    self.step_items = []
    for step_index, text in enumerate(("1  准备素材", "2  确认文字", "3  训练与保存")):
        item = QLabel(text)
        item.setObjectName("TrainingStepItem")
        item.setMinimumHeight(34)
        item.setAlignment(Qt.AlignCenter)
        item.setProperty("active", False)
        step_strip.addWidget(item, 1)
        self.step_items.append(item)
    layout.addLayout(step_strip)

    self.global_status_label = _ow_training_label("请选择训练素材开始。", "TrainingGlobalStatus")
    self.global_status_label.setWordWrap(True)
    layout.addWidget(self.global_status_label)

    self.step_stack = QStackedWidget()
    self.step_stack.setObjectName("TrainingStepStack")
    layout.addWidget(self.step_stack, 1)

    setup_page = QWidget()
    setup_layout = QVBoxLayout(setup_page)
    setup_layout.setContentsMargins(0, 0, 0, 0)
    setup_card = _ow_training_card()
    setup_card_layout = QVBoxLayout(setup_card)
    setup_card_layout.setContentsMargins(18, 16, 18, 16)
    setup_card_layout.setSpacing(8)
    setup_card_layout.addWidget(_ow_training_label("1. 准备训练素材", "TrainingSectionTitle"))
    setup_card_layout.addWidget(_ow_training_hint("建议准备同一个人的清晰语音，先用少量素材试跑，确认流程正常后再增加数量。"))
    form = QHBoxLayout()
    form.setSpacing(18)
    form.setAlignment(Qt.AlignTop)
    avatar_box = QVBoxLayout()
    avatar_box.setSpacing(3)
    avatar_label = _ow_training_label("角色头像", "TrainingFieldLabel")
    avatar_label.setAlignment(Qt.AlignHCenter)
    avatar_box.addWidget(avatar_label)
    avatar_content = QHBoxLayout()
    self.avatar_picker = TrainingAvatarPicker(92)
    self.avatar_picker.clicked.connect(self._ow_training_choose_avatar)
    avatar_content.addWidget(self.avatar_picker, 0, Qt.AlignHCenter | Qt.AlignTop)
    avatar_box.addLayout(avatar_content)
    form.addLayout(avatar_box, 0)
    name_box = QVBoxLayout()
    name_box.setSpacing(3)
    name_box.addWidget(_ow_training_label("模型名称", "TrainingFieldLabel"))
    self.name_input = QLineEdit()
    self.name_input.setPlaceholderText("例如：我的新声音")
    self.name_input.setText("我的新声音")
    self.name_input.setObjectName("TrainingNameInput")
    self.name_input.setFixedWidth(210)
    name_box.addWidget(self.name_input)
    form.addLayout(name_box, 0)
    language_box = QVBoxLayout()
    language_box.setSpacing(3)
    language_box.addWidget(_ow_training_label("训练语音", "TrainingFieldLabel"))
    self.language_label = _ow_training_label("中文", "TrainingFixedLanguage")
    self.language_label.setMinimumHeight(42)
    self.language_label.setFixedWidth(72)
    self.language_label.setAlignment(Qt.AlignCenter)
    language_box.addWidget(self.language_label)
    form.addLayout(language_box, 0)
    form.addStretch(1)
    setup_card_layout.addLayout(form)
    audio_buttons = QHBoxLayout()
    audio_buttons.setSpacing(8)
    self.choose_button = QPushButton("添加音频")
    self.choose_button.setObjectName("TrainingPrimaryButton")
    self.choose_button.clicked.connect(self._choose_audio)
    audio_buttons.addWidget(self.choose_button)
    self.remove_audio_button = QPushButton("删除")
    self.remove_audio_button.setObjectName("TrainingSecondaryButton")
    self.remove_audio_button.clicked.connect(self._ow_training_remove_audio)
    audio_buttons.addWidget(self.remove_audio_button)
    self.clear_audio_button = QPushButton("清空列表")
    self.clear_audio_button.setObjectName("TrainingSecondaryButton")
    self.clear_audio_button.clicked.connect(self._ow_training_clear_audio)
    audio_buttons.addWidget(self.clear_audio_button)
    audio_buttons.addStretch()
    self.tile_view_button = QPushButton("平铺")
    self.tile_view_button.setObjectName("TrainingViewButton")
    self.tile_view_button.setCheckable(True)
    self.tile_view_button.clicked.connect(lambda: self._ow_training_set_file_view("tiles"))
    audio_buttons.addWidget(self.tile_view_button)
    self.column_view_button = QPushButton("两列")
    self.column_view_button.setObjectName("TrainingViewButton")
    self.column_view_button.setCheckable(True)
    self.column_view_button.setChecked(True)
    self.column_view_button.clicked.connect(lambda: self._ow_training_set_file_view("columns"))
    audio_buttons.addWidget(self.column_view_button)
    setup_card_layout.addLayout(audio_buttons)
    self.file_hint = _ow_training_hint("支持 WAV、MP3、FLAC、M4A、OGG 等格式。")
    self.file_hint.setMinimumHeight(22)
    setup_card_layout.addWidget(self.file_hint)
    self.file_list = _TrainingFileList()
    self.file_list.setObjectName("TrainingFileList")
    self.file_list.setSelectionMode(QAbstractItemView.MultiSelection)
    self.file_list.setFocusPolicy(Qt.NoFocus)
    self.file_list.setMinimumHeight(220)
    self.file_list.setMaximumHeight(320)
    self.file_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    self.file_list.set_view_kind("columns")
    setup_card_layout.addWidget(self.file_list, 1)
    self.setup_next_button = QPushButton("下一步")
    self.setup_next_button.setObjectName("TrainingPrimaryButton")
    self.setup_next_button.clicked.connect(self._ow_training_go_to_text)
    setup_card_layout.addWidget(self.setup_next_button, 0, Qt.AlignRight)
    setup_layout.addWidget(setup_card)
    setup_layout.addStretch(1)
    self.step_stack.addWidget(setup_page)

    text_page = QWidget()
    text_layout = QVBoxLayout(text_page)
    text_layout.setContentsMargins(0, 0, 0, 0)
    text_card = _ow_training_card()
    text_card_layout = QVBoxLayout(text_card)
    text_card_layout.setContentsMargins(18, 16, 18, 16)
    text_card_layout.setSpacing(8)
    text_head = QHBoxLayout()
    text_head.addWidget(_ow_training_label("2. 校对训练文字", "TrainingSectionTitle"))
    text_head.addStretch()
    self.transcribe_button = QPushButton("自动识别文字")
    self.transcribe_button.setObjectName("TrainingSecondaryButton")
    self.transcribe_button.clicked.connect(self._transcribe)
    self.save_text_button = QPushButton("保存文字")
    self.save_text_button.setObjectName("TrainingSecondaryButton")
    self.save_text_button.clicked.connect(self._save_text)
    self.save_text_button.setVisible(False)
    text_head.addWidget(self.transcribe_button)
    text_head.addWidget(self.save_text_button)
    text_card_layout.addLayout(text_head)
    text_card_layout.addWidget(_ow_training_hint("自动识别只是初稿，请逐条检查；模型会根据“音频 + 对应文字”学习。"))
    text_actions = QHBoxLayout()
    self.text_back_button = QPushButton("返回上一步")
    self.text_back_button.setObjectName("TrainingSecondaryButton")
    self.text_back_button.clicked.connect(lambda: self._ow_training_navigate(0))
    text_actions.addWidget(self.text_back_button)
    text_actions.addStretch()
    self.transcript_table = QTableWidget(0, 2)
    self.transcript_table.setObjectName("TrainingTranscriptTable")
    self.transcript_table.setHorizontalHeaderLabels(["音频", "训练文本（双击修改）"])
    self.transcript_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    self.transcript_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
    self.transcript_table.verticalHeader().setVisible(False)
    self.transcript_table.verticalHeader().setDefaultSectionSize(44)
    self.transcript_table.verticalHeader().setMinimumSectionSize(42)
    self.transcript_table.setShowGrid(False)
    self.transcript_table.setAlternatingRowColors(True)
    self.transcript_table.setSelectionMode(QAbstractItemView.NoSelection)
    self.transcript_table.setWordWrap(False)
    self.transcript_table.setTextElideMode(Qt.ElideRight)
    self.transcript_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
    self._dirty_rows = set()
    self.transcript_table.itemChanged.connect(self._ow_training_text_changed)
    self.transcript_table.setMinimumHeight(250)
    text_card_layout.addWidget(self.transcript_table, 1)
    self.prepare_button = QPushButton("确认文字并准备数据")
    self.prepare_button.setObjectName("TrainingPrimaryButton")
    self.prepare_button.setEnabled(False)
    self.prepare_button.clicked.connect(self._prepare)
    text_actions.addWidget(self.prepare_button)
    text_card_layout.addLayout(text_actions)
    text_layout.addWidget(text_card, 1)
    self.step_stack.addWidget(text_page)

    train_page = QWidget()
    train_layout = QVBoxLayout(train_page)
    train_layout.setContentsMargins(0, 0, 0, 0)
    action_card = _ow_training_card()
    action_layout = QVBoxLayout(action_card)
    action_layout.setContentsMargins(18, 16, 18, 16)
    action_layout.setSpacing(8)
    action_layout.addWidget(_ow_training_label("3. 训练与保存", "TrainingSectionTitle"))
    self.status_label = _ow_training_label("请选择训练素材开始。", "TrainingStatusLabel")
    self.status_label.setWordWrap(True)
    action_layout.addWidget(self.status_label)
    self.progress = QProgressBar()
    self.progress.setObjectName("TrainingProgress")
    self.progress.setRange(0, 100)
    self.progress.setValue(0)
    self.progress.setTextVisible(False)
    self.progress.setMinimumHeight(10)
    action_layout.addWidget(self.progress)
    action_layout.addWidget(_ow_training_hint("数据准备会处理音频和文本，开始训练后请保持程序运行。"))
    train_buttons = QHBoxLayout()
    self.cancel_button = QPushButton("停止")
    self.cancel_button.setObjectName("TrainingSecondaryButton")
    self.cancel_button.setEnabled(False)
    self.cancel_button.clicked.connect(self._cancel)
    self.train_button = QPushButton("开始/继续训练")
    self.train_button.setObjectName("TrainingPrimaryButton")
    self.train_button.setEnabled(False)
    self.train_button.clicked.connect(self._start_training)
    self.back_text_button = QPushButton("返回上一步")
    self.back_text_button.setObjectName("TrainingSecondaryButton")
    self.back_text_button.clicked.connect(lambda: self._ow_training_navigate(1))
    train_buttons.addWidget(self.back_text_button)
    train_buttons.addStretch()
    self.training_log_button = QPushButton("训练日志")
    self.training_log_button.setObjectName("TrainingSecondaryButton")
    self.training_log_button.setEnabled(False)
    self.training_log_button.setVisible(False)
    self.training_log_button.clicked.connect(self._ow_training_show_log)
    train_buttons.addWidget(self.training_log_button)
    train_buttons.addWidget(self.cancel_button)
    train_buttons.addWidget(self.train_button)
    action_layout.addLayout(train_buttons)
    self.log_view = QTextEdit()
    self.log_view.setObjectName("TrainingLog")
    self.log_view.setReadOnly(True)
    self.save_model_button = QPushButton("保存模型")
    self.save_model_button.setObjectName("TrainingPrimaryButton")
    self.save_model_button.setEnabled(False)
    self.save_model_button.clicked.connect(self._finalize)
    train_buttons.addWidget(self.save_model_button)
    # 详细日志写入任务目录，界面只保留状态与进度，避免黑色日志框挤压操作区。
    self.log_view.setVisible(False)
    self.log_view.setMinimumHeight(0)
    self.log_view.setMaximumHeight(0)
    action_layout.addWidget(self.log_view)
    train_layout.addWidget(action_card, 1)
    self.step_stack.addWidget(train_page)
    self.done_label = _ow_training_hint("训练完成后，可以保存到本地模型库。")
    self._ow_training_set_step(0)


def _ow_training_set_step(self, index: int) -> None:
    index = max(0, min(index, self.step_stack.count() - 1))
    self.step_stack.setCurrentIndex(index)
    names = ("准备训练素材", "确认训练文字", "训练与保存")
    self.step_label.setText(f"当前步骤：{names[index]}")
    for item_index, item in enumerate(self.step_items):
        item.setProperty("active", item_index == index)
        item.style().unpolish(item)
        item.style().polish(item)


def _ow_training_set_file_view(self, kind: str) -> None:
    kind = "tiles" if kind == "tiles" else "columns"
    self.tile_view_button.setChecked(kind == "tiles")
    self.column_view_button.setChecked(kind == "columns")
    self.file_list.set_view_kind(kind)


def _ow_training_show_log(self) -> None:
    if not self.job_id:
        return
    if self.training_log_dialog is not None:
        self.training_log_dialog.show()
        self.training_log_dialog.raise_()
        self.training_log_dialog.activateWindow()
        self._ow_training_refresh_log()
        return
    dialog = QDialog(self)
    dialog.setObjectName("TrainingLogDialog")
    dialog.setWindowTitle("训练日志")
    dialog.resize(720, 440)
    dialog_layout = QVBoxLayout(dialog)
    dialog_layout.setContentsMargins(18, 16, 18, 16)
    dialog_layout.setSpacing(10)
    dialog_layout.addWidget(_ow_training_label("训练日志", "TrainingLogDialogTitle"))
    dialog_layout.addWidget(_ow_training_hint("日志会自动刷新；训练进行中无需反复关闭和打开。"))
    # 训练日志是纯文本且可能持续增长，QPlainTextEdit 比 QTextEdit 更适合高频更新。
    text_edit = QPlainTextEdit()
    text_edit.setObjectName("TrainingLogDialogText")
    text_edit.setReadOnly(True)
    text_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
    dialog_layout.addWidget(text_edit, 1)
    footer = QHBoxLayout()
    copy_status = _ow_training_hint("")
    footer.addWidget(copy_status)
    footer.addStretch()
    copy_button = QPushButton("一键复制")
    copy_button.setObjectName("TrainingSecondaryButton")
    copy_button.clicked.connect(lambda: (QApplication.clipboard().setText(text_edit.toPlainText()), copy_status.setText("已复制")))
    footer.addWidget(copy_button)
    close_button = QPushButton("关闭")
    close_button.setObjectName("TrainingPrimaryButton")
    close_button.clicked.connect(dialog.close)
    footer.addWidget(close_button)
    dialog_layout.addLayout(footer)
    self.training_log_dialog = dialog
    self.training_log_text_edit = text_edit
    self._training_log_cache = ""
    dialog.finished.connect(self._ow_training_log_dialog_closed)
    dialog.show()
    self.training_log_refresh_timer.start()
    self._ow_training_refresh_log()


def _ow_training_refresh_log(self) -> None:
    if not self.job_id or self.request_thread is not None or self.training_log_dialog is None:
        return
    self._request("GET", f"/api/training/jobs/{self.job_id}/log", None, self._ow_training_log_loaded)


def _ow_training_log_loaded(self, result: object) -> None:
    log_text = str(result.get("log", "当前任务还没有训练日志。")) if isinstance(result, dict) else "当前任务还没有训练日志。"
    # 对话框关闭后，已经发出的异步请求返回时不能重新弹出旧窗口。
    if self.training_log_dialog is None or self.training_log_text_edit is None:
        return
    text_edit = self.training_log_text_edit
    if log_text == self._training_log_cache:
        return

    scrollbar = text_edit.verticalScrollBar()
    old_value = scrollbar.value()
    follow_tail = scrollbar.maximum() <= 0 or old_value >= scrollbar.maximum() - 8
    self._training_log_cache = log_text
    text_edit.setUpdatesEnabled(False)
    try:
        text_edit.setPlainText(log_text)
    finally:
        text_edit.setUpdatesEnabled(True)

    def restore_scroll_position() -> None:
        # 对话框可能在异步请求返回前被关闭，不能再操作旧控件。
        if self.training_log_text_edit is not text_edit:
            return
        try:
            current_scrollbar = text_edit.verticalScrollBar()
            if follow_tail:
                current_scrollbar.setValue(current_scrollbar.maximum())
            else:
                current_scrollbar.setValue(min(old_value, current_scrollbar.maximum()))
        except RuntimeError:
            # Qt 对象已被销毁时，异步回调应安全退出。
            return

    # 文本重新布局在当前事件循环结束后才会更新 maximum。
    QTimer.singleShot(0, restore_scroll_position)


def _ow_training_log_dialog_closed(self) -> None:
    self.training_log_refresh_timer.stop()
    self.training_log_dialog = None
    self.training_log_text_edit = None
    self._training_log_cache = ""


def _ow_training_step_for_job(self) -> int:
    if not self.job_id:
        return 0
    next_action = str(getattr(self, "_next_action", ""))
    if next_action in {"train", "save", "done"}:
        return 2
    if self.job_status in {"preparing", "running", "cancelling"}:
        return 2
    return 1


def _ow_training_go_to_text(self) -> None:
    if not self.job_id:
        self.status_label.setText("请先选择并上传训练素材。")
        self._ow_training_set_step(0)
        return
    self._ow_training_set_step(1)


def _ow_training_choose_audio(self) -> None:
    paths, _ = QFileDialog.getOpenFileNames(
        self,
        "选择训练素材",
        str(PROJECT_DIR),
        "音频文件 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.wma)",
    )
    if not paths:
        return
    if self.job_id:
        if self.job_status in {"transcribing", "preparing", "running", "cancelling", "registered"}:
            self._ow_training_set_status("当前任务正在处理，完成后才能修改训练素材。")
            return
        self._ow_training_set_status(f"正在追加 {len(paths)} 个音频……")
        self._request(
            "POST",
            f"/api/training/jobs/{self.job_id}/files",
            {"source_paths": paths},
            self._ow_training_files_changed,
        )
        return
    self.job_status = ""
    self._table_dirty = False
    self._dirty_rows.clear()
    self.progress.setValue(0)
    self.log_view.clear()
    self.transcript_table.setRowCount(0)
    self.source_paths = paths
    self.file_list.clear()
    for path in paths:
        self.file_list.addItem(Path(path).name)
    self.file_hint.setText(f"正在导入 {len(paths)} 个音频……")
    self._ow_training_set_step(0)
    self._create_job()


def _ow_training_files_changed(self, job: object) -> None:
    if not isinstance(job, dict):
        return
    self._table_dirty = False
    self._dirty_rows.clear()
    self._ow_training_render_job(job)
    self._ow_training_set_step(0)
    self._ow_training_set_status(str(job.get("message", f"当前有 {len(job.get('files', []))} 个音频。")))


def _ow_training_choose_avatar(self) -> None:
    path, _ = QFileDialog.getOpenFileName(
        self,
        "选择角色头像",
        str(PROJECT_DIR),
        "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
    )
    if not path:
        return
    pixmap = QPixmap(path)
    if pixmap.isNull():
        self._ow_training_set_status("头像无法读取，请换一张图片。")
        return
    self.avatar_path = path
    self.avatar_picker.set_avatar_pixmap(pixmap)
    if not self.job_id:
        self._ow_training_set_status("头像已选择，添加音频后会一起保存。")
        return
    if self.job_status in {"transcribing", "preparing", "running", "cancelling", "registered"}:
        self._ow_training_set_status("当前任务正在处理，完成后才能修改角色头像。")
        return
    self._ow_training_set_status("正在保存角色头像……")
    self._request(
        "POST",
        f"/api/training/jobs/{self.job_id}/avatar",
        {"source_path": path},
        self._ow_training_avatar_changed,
    )


def _ow_training_avatar_changed(self, job: object) -> None:
    if not isinstance(job, dict):
        return
    self._ow_training_render_job(job)
    self._ow_training_set_status("角色头像已保存。")


def _ow_training_load_avatar(self) -> None:
    if not self.job_id or self.avatar_preview_thread is not None:
        return
    if self.avatar_picker.preview_pixmap.isNull() and getattr(self, "_avatar_available", False):
        self.avatar_preview_thread = QThread(self)
        self.avatar_preview_worker = ModelPreviewWorker(
            [(self.job_id, f"{self.api_url}/api/training/jobs/{self.job_id}/avatar")]
        )
        self.avatar_preview_worker.moveToThread(self.avatar_preview_thread)
        self.avatar_preview_thread.started.connect(self.avatar_preview_worker.run)
        self.avatar_preview_worker.loaded.connect(self._ow_training_avatar_loaded)
        self.avatar_preview_worker.finished.connect(self.avatar_preview_thread.quit)
        self.avatar_preview_worker.finished.connect(self.avatar_preview_worker.deleteLater)
        self.avatar_preview_thread.finished.connect(self._ow_training_avatar_preview_finished)
        # avatar_preview_thread 由训练页关闭流程统一回收，避免头像请求完成后的竞态删除。
        self.avatar_preview_thread.start()


def _ow_training_avatar_loaded(self, _job_id: str, data: bytes) -> None:
    pixmap = QPixmap()
    if pixmap.loadFromData(data):
        self.avatar_picker.set_avatar_pixmap(pixmap)


def _ow_training_avatar_preview_finished(self) -> None:
    finished_thread = self.avatar_preview_thread
    self.avatar_preview_thread = None
    self.avatar_preview_worker = None
    _ow_delete_finished_thread(finished_thread)


def _ow_training_reset_preview_buttons(self) -> None:
    for row in range(self.transcript_table.rowCount()):
        cell = self.transcript_table.cellWidget(row, 0)
        button = cell.findChild(QPushButton, "TrainingPreviewButton") if cell else None
        if button is not None:
            button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))


def _ow_training_play_audio(self, row: int) -> None:
    if row < 0 or row >= len(self._training_file_names) or not self.job_id:
        return
    file_name = self._training_file_names[row]
    if self.preview_name == file_name and self.preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
        self.preview_player.stop()
        self._ow_training_reset_preview_buttons()
        self.preview_name = ""
        self._ow_training_set_status("已停止试听。")
        return
    self.preview_player.stop()
    self._ow_training_reset_preview_buttons()
    self.preview_name = file_name
    cell = self.transcript_table.cellWidget(row, 0)
    button = cell.findChild(QPushButton, "TrainingPreviewButton") if cell else None
    if button is not None:
        button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
    url = f"{self.api_url}/api/training/jobs/{self.job_id}/files/{quote(file_name, safe='')}/audio"
    self.preview_player.setSource(QUrl(url))
    self.preview_player.play()
    self._ow_training_set_status(f"正在试听：{file_name}")


def _ow_training_preview_status(self, status) -> None:
    if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
        for row, file_name in enumerate(self._training_file_names):
            if file_name == self.preview_name:
                cell = self.transcript_table.cellWidget(row, 0)
                button = cell.findChild(QPushButton, "TrainingPreviewButton") if cell else None
                if button is not None:
                    button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
                break
    elif status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
        self._ow_training_reset_preview_buttons()
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._ow_training_set_status("试听播放完成。")
        self.preview_name = ""


def _ow_training_preview_error(self, _error, error_string: str) -> None:
    self._ow_training_reset_preview_buttons()
    self.preview_name = ""
    self._ow_training_set_status(f"试听失败：{error_string or '无法打开音频'}")


def _ow_training_remove_audio(self) -> None:
    selected = self.file_list.selectedItems()
    if not selected or not self.job_id:
        self._ow_training_set_status("请先在列表中选中要删除的音频。")
        return
    file_names = [str(item.data(Qt.UserRole) or item.text().split("  ·  ", 1)[0]) for item in selected]
    answer = QMessageBox.question(
        self,
        "删除训练素材",
        f"确定删除选中的 {len(file_names)} 个音频吗？删除后需要重新准备训练数据。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    self._ow_training_set_status(f"正在删除 {len(file_names)} 个音频……")
    self._request(
        "POST",
        f"/api/training/jobs/{self.job_id}/files/remove",
        {"file_names": file_names},
        self._ow_training_files_changed,
    )


def _ow_training_clear_audio(self) -> None:
    if not self.job_id:
        self.file_list.clear()
        self.source_paths = []
        self.transcript_table.setRowCount(0)
        self._ow_training_set_status("素材列表已清空，请重新添加音频。")
        return
    if self.file_list.count() == 0:
        return
    if self.job_status in {"transcribing", "preparing", "running", "cancelling", "registered"}:
        self._ow_training_set_status("当前任务正在处理，完成后才能清空训练素材。")
        return
    answer = QMessageBox.question(
        self,
        "清空训练素材",
        "确定清空当前训练任务的全部音频吗？此操作不可恢复。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    self._ow_training_set_status("正在清空训练素材……")
    self._request(
        "DELETE",
        f"/api/training/jobs/{self.job_id}/files",
        None,
        self._ow_training_files_changed,
    )


def _ow_training_set_status(self, text: str) -> None:
    """在四个步骤都可见的位置显示当前任务状态。"""
    text = re.sub(r"[.…]+$", "", str(text or "")).rstrip()
    self._status_base_text = text
    self._status_dot_count = 0
    is_error = "失败" in text or text.startswith("操作失败")
    for label in (self.global_status_label, self.status_label):
        label.setProperty("error", is_error)
        label.setText(text)
        label.style().unpolish(label)
        label.style().polish(label)


def _ow_training_animate_status(self) -> None:
    if not self._status_base_text:
        return
    self._status_dot_count = (self._status_dot_count % 3) + 1
    text = self._status_base_text + ("." * self._status_dot_count)
    self.global_status_label.setText(text)
    self.status_label.setText(text)


def _ow_training_create_job(self) -> None:
    if not self.source_paths:
        return
    self._ow_training_set_status("正在导入音频并创建训练任务……")
    payload = {
        "name": self.name_input.text().strip(),
        "language": "zh",
        "source_paths": self.source_paths,
        "avatar_path": self.avatar_path or None,
    }
    self._request("POST", "/api/training/jobs", payload, self._job_created)


def _ow_training_render_job(self, job: dict) -> None:
    self.job_status = str(job.get("status", ""))
    self._ow_training_update_heartbeat()
    self._last_status_stage = str(job.get("stage", ""))
    files = [item for item in job.get("files", []) if isinstance(item, dict)]
    self._next_action = str(job.get("next_action", ""))
    if not self._next_action:
        if self.job_status == "draft":
            self._next_action = "prepare" if any(str(item.get("text", "")).strip() for item in files) else "transcribe"
        elif self.job_status in {"preparing", "running", "cancelling"}:
            self._next_action = "wait"
        elif self.job_status in {"completed", "registered"}:
            self._next_action = "save" if self.job_status == "completed" else "done"
        else:
            self._next_action = "prepare"
    self._can_train = bool(job.get("can_train", self._next_action == "train"))
    self._avatar_available = bool(str(job.get("avatar_name", "")).strip())
    self._training_file_names = [str(item.get("name", "")) for item in files]
    if self._avatar_available:
        self._ow_training_load_avatar()
    self.file_list.clear()
    for row, item in enumerate(files):
        name = str(item.get("name", item.get("source_name", "")))
        display_name = str(item.get("source_name", name))
        duration = float(item.get("duration", 0) or 0)
        label = f"{row + 1:02d}  {display_name}  ·  {duration:.1f} 秒" if duration > 0 else f"{row + 1:02d}  {display_name}"
        list_item = QListWidgetItem(label)
        list_item.setToolTip(display_name)
        list_item.setData(Qt.UserRole, name)
        self.file_list.addItem(list_item)
    self.file_list.refresh_layout()
    dirty_rows = getattr(self, "_dirty_rows", set())
    self.transcript_table.blockSignals(True)
    try:
        self.transcript_table.setRowCount(len(files))
        for row, item in enumerate(files):
            internal_name = str(item.get("name", ""))
            display_name = str(item.get("source_name", internal_name))
            audio_cell = QWidget()
            audio_layout = QHBoxLayout(audio_cell)
            audio_layout.setContentsMargins(8, 2, 8, 2)
            audio_layout.setSpacing(6)
            audio_label = _ElidingLabel(display_name)
            audio_label.setToolTip(display_name)
            audio_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            audio_layout.addWidget(audio_label, 1)
            play_button = QPushButton()
            play_button.setObjectName("TrainingPreviewButton")
            play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            play_button.setIconSize(QSize(16, 16))
            play_button.setFixedSize(30, 28)
            play_button.setToolTip("试听")
            play_button.clicked.connect(lambda _checked=False, index=row: self._ow_training_play_audio(index))
            audio_layout.addWidget(play_button)
            self.transcript_table.setCellWidget(row, 0, audio_cell)
            text = self.transcript_table.item(row, 1)
            if text is None:
                text = QTableWidgetItem()
                text.setTextAlignment(Qt.AlignCenter)
                self.transcript_table.setItem(row, 1, text)
            if row not in dirty_rows:
                text.setText(str(item.get("text", "")))
            text.setToolTip(text.text())
            text.setTextAlignment(Qt.AlignCenter)
    finally:
        self.transcript_table.blockSignals(False)
    progress = int(job.get("progress", 0) or 0)
    self.progress.setValue(max(0, min(100, progress)))
    message = str(job.get("message", job.get("stage", "")))
    message = message.replace("请展开训练日志查看详情。", "请点击“训练日志”查看详情。")
    message = message.replace("请展开训练日志确认训练是否真正完成。", "请点击“训练日志”查看详情。")
    self._ow_training_set_status(message)
    if self.job_status == "draft" and files and any(str(item.get("text", "")).strip() for item in files):
        self.transcribe_button.setText("重新识别文字")
    else:
        self.transcribe_button.setText("自动识别文字")
    if self.job_status == "draft" and files:
        recognized = any(str(item.get("text", "")).strip() for item in files)
        if recognized:
            self.file_hint.setText(f"导入成功 · {len(files)} 个音频；识别文字已生成，请检查。")
        else:
            self.file_hint.setText(f"导入成功 · {len(files)} 个音频；请进入下一步填写或识别文字。")
    elif self.job_status in {"transcribing", "preparing", "running", "cancelling"} and files:
        self.file_hint.setText(f"素材已导入 · {len(files)} 个音频；{message}")
    elif self.job_status in {"failed", "interrupted"}:
        self.file_hint.setText(f"素材仍保留 · {len(files)} 个音频；请查看上方状态后继续。")
    if self.job_status in {"completed", "registered"}:
        self.done_label.setText(str(job.get("message", "模型训练完成，可以保存到本地模型库。")))
        if message != getattr(self, "_last_status_message", ""):
            self.log_view.append(message)
    self._last_status_message = message
    QTimer.singleShot(0, self._ow_training_finish_pending_navigation)


def _ow_training_update_heartbeat(self) -> None:
    active = self.job_status in {"transcribing", "preparing", "running", "cancelling"}
    if active:
        if not self.training_heartbeat_timer.isActive():
            self.training_heartbeat_timer.start()
    else:
        self.training_heartbeat_timer.stop()


def _ow_training_send_heartbeat(self) -> None:
    if self.job_status not in {"transcribing", "preparing", "running", "cancelling"}:
        self.training_heartbeat_timer.stop()
        return
    if not self.job_id or self.request_thread is not None:
        return
    self._request(
        "POST",
        "/api/training/session/heartbeat",
        {"job_id": self.job_id},
        lambda _result: None,
    )


def _ow_training_confirm_stop(self, action: str) -> None:
    if self._pending_training_navigation is not None:
        return
    text = (
        "当前正在训练，是否停止并退出？"
        if action == "close"
        else "当前正在训练，是否停止并返回？"
    )
    dialog = QMessageBox(self)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setWindowTitle("确认操作")
    dialog.setText(text)
    dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    dialog.button(QMessageBox.StandardButton.Yes).setText("确定")
    dialog.button(QMessageBox.StandardButton.No).setText("取消")
    if dialog.exec() != QMessageBox.StandardButton.Yes:
        return
    self._pending_training_navigation = action
    self._ow_training_set_status("正在停止训练，请稍候")
    self._ow_training_wait_for_cancel_slot()


def _ow_training_wait_for_cancel_slot(self) -> None:
    if self._pending_training_navigation is None:
        return
    if self.request_thread is not None:
        QTimer.singleShot(100, self._ow_training_wait_for_cancel_slot)
        return
    _ow_training_original_cancel(self)


def _ow_training_finish_pending_navigation(self) -> None:
    action = self._pending_training_navigation
    if action is None or self.job_status in {"transcribing", "preparing", "running", "cancelling"}:
        return
    self._pending_training_navigation = None
    self.training_heartbeat_timer.stop()
    owner = self.window()
    if owner is None:
        return
    if action == "close":
        owner.close()
    else:
        self.back_requested.emit()


def _ow_training_can_close(self) -> bool:
    if self._pending_training_navigation is not None:
        return False
    if self.job_status in {"transcribing", "preparing", "running", "cancelling"}:
        self._ow_training_confirm_stop("close")
        return False
    return True


def _ow_training_action_started(self, job: object) -> None:
    """只有明确启动了识别、准备或训练时才切换阶段，普通刷新不导航。"""
    if isinstance(job, dict):
        self._ow_training_render_job(job)
        action = str(job.get("next_action", ""))
        if action in {"wait", "train", "save", "done"} and str(job.get("status", "")) != "transcribing":
            self._ow_training_set_step(2)
        elif str(job.get("status", "")) == "transcribing":
            self._ow_training_set_step(1)
    if self.job_id:
        self.poll_timer.start()
    self._ow_training_set_buttons()


def _ow_training_job_created(self, job: object) -> None:
    if not isinstance(job, dict):
        return
    self._training_runtime_released = False
    self.job_id = str(job.get("id", "")) or None
    self._ow_training_render_job(job)
    self.log_view.append(str(job.get("message", "训练任务已创建。")))
    self._ow_training_set_status("导入成功 · 素材已保存到本地训练任务，请点击下一步校对文字。")
    self._ow_training_set_step(0)
    self._ow_training_set_buttons()


def _ow_training_request(self, method: str, path: str, payload: dict | None, callback) -> None:
    if self.request_thread is not None:
        return
    self.request_thread = QThread(self)
    self._request_path = path
    self.request_worker = TrainingApiWorker(
        method,
        f"{self.api_url}{path}",
        payload,
        {"X-OwVoice-Session": self.session_id},
    )
    self.request_callback = callback
    self.request_worker.moveToThread(self.request_thread)
    self.request_thread.started.connect(self.request_worker.run)
    self.request_worker.loaded.connect(self._ow_training_request_loaded)
    self.request_worker.failed.connect(self._ow_training_request_failed)
    self.request_worker.finished.connect(self.request_thread.quit)
    self.request_worker.finished.connect(self.request_worker.deleteLater)
    self.request_thread.finished.connect(self._ow_training_request_finished)
    # request_thread 由 TrainingPage 的 closeEvent 统一回收，避免失效引用。
    self.request_thread.start()
    self._ow_training_set_buttons()


def _ow_training_request_loaded(self, data: object) -> None:
    callback = self.request_callback
    if callback is None:
        return
    try:
        callback(data)
    except Exception as exc:  # noqa: BLE001 - 信号槽异常不能导致窗口直接退出
        self._ow_training_frontend_error("处理训练接口响应失败", exc)


def _ow_training_request_finished(self) -> None:
    finished_thread = self.request_thread
    self.request_thread = None
    self.request_worker = None
    # 先清空页面引用，再回收已结束的线程，避免关闭窗口时访问失效 QThread。
    if finished_thread is not None:
        try:
            finished_thread.deleteLater()
        except RuntimeError:
            pass
    try:
        self._ow_training_set_buttons()
    except Exception as exc:  # noqa: BLE001 - 最后一道 UI 保护
        self._ow_training_frontend_error("刷新训练界面失败", exc)


def _ow_training_request_failed(self, message: str) -> None:
    if str(getattr(self, "_request_path", "")) == "/api/training/session/heartbeat":
        self._ow_training_set_status(f"训练会话心跳失败：{message}")
        self._ow_training_set_buttons()
        return
    if str(getattr(self, "_request_path", "")).endswith("/cancel") and self._pending_training_navigation is not None:
        self._pending_training_navigation = None
        self._ow_training_set_status(f"停止训练失败：{message}，请重试")
    self.poll_timer.stop()
    self._ow_training_set_status(f"操作失败：{message}")
    self.log_view.append(f"操作失败：{message}")
    if not self.job_id:
        self.file_hint.setText(f"导入失败：{message}。请检查文件后重试。")
    self._ow_training_set_buttons()


def _ow_training_frontend_error(self, context: str, exc: BaseException) -> None:
    log_path = _ow_write_frontend_error(context, exc)
    try:
        self.poll_timer.stop()
        self.status_label.setText(f"界面处理失败，任务数据已保留。日志：{log_path}")
        self.log_view.append(f"{context}：{exc}")
        self._ow_training_set_buttons()
    except Exception as nested_exc:  # noqa: BLE001 - 日志保护本身不能再抛出
        _ow_write_frontend_error("显示前端错误信息失败", nested_exc)


def _ow_training_restore_latest_job(self) -> None:
    if self.job_id or self.request_thread is not None:
        return
    if self.training_setup_thread is not None:
        QTimer.singleShot(100, self._ow_training_restore_latest_job)
        return
    self.status_label.setText("正在检查上次未完成的训练任务……")
    self._request("GET", "/api/training/jobs/recent", None, self._ow_training_recent_job_loaded)


def _ow_training_prepare_environment(self) -> None:
    """进入训练页时先检查环境；缺少可选组件时询问用户是否安装。"""
    if self.training_setup_thread is not None or self.request_thread is not None:
        return
    self._ow_training_set_status("正在检查本地训练环境……")
    self._request("GET", "/api/training/environment", None, self._ow_training_environment_loaded)


def _ow_training_environment_loaded(self, result: object) -> None:
    if not isinstance(result, dict):
        self._ow_training_set_status("无法读取本地训练环境检查结果。")
        return
    self.training_environment_ready = bool(result.get("ready"))
    if self.training_environment_ready:
        self._ow_training_set_status("本地训练环境已就绪。")
        # API worker 发送 loaded 信号时自身尚未发出 finished，稍后再发起下一次请求。
        QTimer.singleShot(100, self._ow_training_restore_latest_job)
        return

    missing = [str(item) for item in result.get("missingDependencies", [])]
    missing.extend(str(item) for item in result.get("missingFiles", []))
    details = "\n".join(missing[:8])
    if len(missing) > 8:
        details += f"\n……还有 {len(missing) - 8} 项……"
    answer = QMessageBox.question(
        self,
        "安装本地训练组件",
        "本地训练需要额外的 Python 依赖和预训练模型。安装过程需要网络，并会占用额外磁盘空间。\n\n"
        + (details or "当前环境检查未通过。")
        + "\n\n现在安装吗？",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    if answer == QMessageBox.Yes:
        self._ow_training_start_setup()
    else:
        self._ow_training_set_status("已跳过训练环境安装；再次打开本地训练时可以重新安装。")
        QTimer.singleShot(100, self._ow_training_restore_latest_job)


def _ow_training_start_setup(self) -> None:
    if self.training_setup_thread is not None:
        return
    self.training_environment_ready = False
    self._ow_training_set_status("正在安装训练组件，请保持程序运行……")
    self.log_view.append("开始安装本地训练依赖和预训练模型。")
    self.training_setup_thread = QThread(self)
    self.training_setup_worker = TrainingSetupWorker()
    self.training_setup_worker.moveToThread(self.training_setup_thread)
    self.training_setup_thread.started.connect(self.training_setup_worker.run)
    self.training_setup_worker.output.connect(self._ow_training_setup_output)
    self.training_setup_worker.success.connect(self._ow_training_setup_success)
    self.training_setup_worker.failed.connect(self._ow_training_setup_failed)
    self.training_setup_worker.finished.connect(self.training_setup_thread.quit)
    self.training_setup_worker.finished.connect(self.training_setup_worker.deleteLater)
    self.training_setup_thread.finished.connect(self._ow_training_setup_finished)
        # training_setup_thread 由训练页关闭流程统一回收，避免安装完成后的竞态删除。
    self.training_setup_thread.start()
    self._ow_training_set_buttons()


def _ow_training_setup_output(self, line: str) -> None:
    self.log_view.append(line)
    self._ow_training_set_status(line)


def _ow_training_setup_success(self) -> None:
    self._ow_training_set_status("训练组件安装完成，正在重新检查环境……")
    self._request("GET", "/api/training/environment", None, self._ow_training_environment_loaded)


def _ow_training_setup_failed(self, message: str) -> None:
    self.training_environment_ready = False
    self.log_view.append(f"训练环境安装失败：{message}")
    self._ow_training_set_status(f"训练环境安装失败：{message}")
    QMessageBox.warning(self, "训练环境安装失败", message)


def _ow_training_setup_finished(self) -> None:
    finished_thread = self.training_setup_thread
    self.training_setup_thread = None
    self.training_setup_worker = None
    if finished_thread is not None:
        try:
            finished_thread.deleteLater()
        except RuntimeError:
            pass
    self._ow_training_set_buttons()


def _ow_training_recent_job_loaded(self, result: object) -> None:
    if not isinstance(result, dict):
        return
    job = result.get("job")
    if not isinstance(job, dict):
        self.status_label.setText("请选择训练素材开始。")
        self._ow_training_set_step(0)
        return
    self.job_id = str(job.get("id", "")) or None
    self.name_input.setText(str(job.get("name", "我的新声音")))
    self._ow_training_render_job(job)
    self._next_action = str(job.get("next_action", ""))
    self._ow_training_set_step(self._ow_training_step_for_job())
    if self.job_status in {"completed", "registered"}:
        # _render_job 已经显示了后端返回的完成/保存状态；不能再用“已恢复任务”覆盖，
        # 否则用户会看不到训练确实已经完成。
        if self.job_status == "completed":
            self._ow_training_set_status("训练已完成，可以保存到本地模型库。")
        else:
            self._ow_training_set_status("模型已保存，可以返回工作台使用。")
    else:
        self.log_view.append("已恢复上次未完成的本地训练任务。")
        self._ow_training_set_status("已恢复任务，可以从当前步骤继续。")
    if self.job_status in {"transcribing", "preparing", "running", "cancelling"}:
        self.poll_timer.start()
    self._ow_training_set_buttons()


def _ow_training_text_changed(self, item: QTableWidgetItem) -> None:
    if item.column() != 1:
        return
    self._dirty_rows.add(item.row())
    self._table_dirty = True
    item.setToolTip(item.text())
    self._ow_training_set_status("文字已修改，点击“确认文字并准备数据”即可继续。")


def _ow_training_navigate(self, index: int) -> None:
    current = self.step_stack.currentIndex()
    if index == current:
        return
    if self.request_thread is not None or self.job_status in {"transcribing", "preparing", "running", "cancelling"}:
        self._ow_training_set_status("当前任务正在处理，请等待完成后再切换步骤。")
        return
    if index > 0 and not self.job_id:
        self._ow_training_set_status("请先导入训练素材。")
        return
    if index == 2 and self.job_status not in {"prepared", "completed", "registered"} and not self._can_train:
        self._ow_training_set_status("请先确认文字并完成数据准备。")
        return
    self._ow_training_set_step(index)


def _ow_training_retry(self) -> None:
    if self.job_status not in {"failed", "interrupted"} or self.request_thread is not None:
        return
    next_action = str(getattr(self, "_next_action", ""))
    stage = str(getattr(self, "_last_status_stage", ""))
    if next_action == "transcribe" or "识别" in stage:
        self._transcribe()
    elif next_action == "train":
        self._start_training()
    else:
        self._prepare()


def _ow_training_set_buttons(self) -> None:
    busy = self.request_thread is not None
    setup_busy = self.training_setup_thread is not None
    has_job = bool(self.job_id)
    has_files = self.file_list.count() > 0
    running = self.job_status in {"transcribing", "preparing", "running", "cancelling"}
    locked = self.job_status in {"completed", "registered"}
    can_transcribe = has_files and self.job_status in {"draft", "failed", "interrupted", "prepared"}
    environment_ready = self.training_environment_ready
    self.choose_button.setEnabled(environment_ready and not busy and not setup_busy and not locked)
    self.avatar_picker.setEnabled(environment_ready and not busy and not setup_busy and not running and not locked)
    self.remove_audio_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and has_files and not running and not locked)
    self.clear_audio_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and has_files and not running and not locked)
    self.setup_next_button.setEnabled(environment_ready and has_job and has_files and not busy and not setup_busy and not running)
    self.transcribe_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running and not locked and can_transcribe)
    self.save_text_button.setEnabled(False)
    self.prepare_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running and not locked and self._next_action == "prepare")
    self.train_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running and bool(getattr(self, "_can_train", False)))
    self.save_model_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running and self.job_status == "completed")
    self.cancel_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and running)
    log_available = has_job and self.job_status in {"preparing", "prepared", "running", "failed", "interrupted", "completed", "registered"}
    self.training_log_button.setVisible(log_available)
    self.training_log_button.setEnabled(log_available and not busy and not setup_busy)
    self.text_back_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running)
    self.back_text_button.setEnabled(environment_ready and has_job and not busy and not setup_busy and not running)
    if busy or setup_busy or running:
        if not self.status_animation_timer.isActive():
            self.status_animation_timer.start()
    else:
        self.status_animation_timer.stop()
        if self._status_base_text:
            self.global_status_label.setText(self._status_base_text)
            self.status_label.setText(self._status_base_text)


def _ow_training_text_saved(self, job: object, after=None) -> None:
    self._table_dirty = False
    self._dirty_rows.clear()
    if isinstance(job, dict):
        self._ow_training_render_job(job)
    if not after:
        self._ow_training_set_buttons()
        return

    def continue_after_save() -> None:
        if self.request_thread is not None:
            QTimer.singleShot(50, continue_after_save)
            return
        after()

    QTimer.singleShot(50, continue_after_save)


def _ow_training_prepare(self) -> None:
    if not self.job_id:
        self._ow_training_set_status("请先导入训练素材。")
        return
    transcripts = []
    for row in range(self.transcript_table.rowCount()):
        text_item = self.transcript_table.item(row, 1)
        transcripts.append({
            "name": self._training_file_names[row] if row < len(self._training_file_names) else "",
            "text": text_item.text().strip() if text_item else "",
        })
    self._ow_training_set_status("正在确认文字并准备训练数据……")
    self._request(
        "POST",
        f"/api/training/jobs/{self.job_id}/prepare",
        {"transcripts": transcripts},
        self._action_started,
    )


def _ow_training_start_prepare_request(self) -> None:
    if not self.job_id:
        return
    self._ow_training_set_status("正在准备训练数据……")
    self._request("POST", f"/api/training/jobs/{self.job_id}/prepare", None, self._action_started)


_ow_training_original_transcribe = TrainingPage._transcribe
_ow_training_original_start_training = TrainingPage._start_training
_ow_training_original_finalize = TrainingPage._finalize
_ow_training_original_cancel = TrainingPage._cancel


def _ow_training_transcribe(self) -> None:
    self._ow_training_set_status("正在启动自动识别；识别结果会逐条填入表格……")
    _ow_training_original_transcribe(self)


def _ow_training_start_training(self) -> None:
    if not getattr(self, "_training_runtime_released", False):
        # 先让后端卸载当前角色权重，再停止 GPT-SoVITS 进程；训练只保留
        # OwVoice 后端，显著降低 8GB 显存设备上的资源峰值。
        self._ow_training_set_status("正在释放语音引擎，为本地训练准备资源……")
        self._request(
            "POST",
            "/api/engine/unload",
            None,
            self._ow_training_engine_released,
        )
        return
    self._ow_training_set_status("正在启动模型训练……")
    _ow_training_original_start_training(self)


def _ow_training_engine_released(self, _result: object) -> None:
    self._training_runtime_released = True
    owner = self.window()
    if owner is not None and hasattr(owner, "stop_inference_service_for_training"):
        owner.stop_inference_service_for_training()
    self._ow_training_set_status("语音引擎已释放，正在启动模型训练……")
    _ow_training_original_start_training(self)


def _ow_training_finalize(self) -> None:
    self._ow_training_set_status("正在整理模型文件并保存到本地模型库……")
    _ow_training_original_finalize(self)


def _ow_training_cancel(self) -> None:
    self._ow_training_set_status("正在停止当前任务……")
    _ow_training_original_cancel(self)


TrainingPage._build_ui = _ow_training_build_ui
TrainingPage._choose_audio = _ow_training_choose_audio
TrainingPage._create_job = _ow_training_create_job
TrainingPage._render_job = _ow_training_render_job
TrainingPage._ow_training_render_job = _ow_training_render_job
TrainingPage._job_created = _ow_training_job_created
TrainingPage._request = _ow_training_request
TrainingPage._request_loaded = _ow_training_request_loaded
TrainingPage._request_finished = _ow_training_request_finished
TrainingPage._request_failed = _ow_training_request_failed
TrainingPage._set_buttons = _ow_training_set_buttons
TrainingPage._frontend_error = _ow_training_frontend_error
TrainingPage._ow_training_frontend_error = _ow_training_frontend_error
TrainingPage._ow_training_set_status = _ow_training_set_status
TrainingPage._ow_training_set_step = _ow_training_set_step
TrainingPage._ow_training_set_file_view = _ow_training_set_file_view
TrainingPage._ow_training_update_heartbeat = _ow_training_update_heartbeat
TrainingPage._ow_training_send_heartbeat = _ow_training_send_heartbeat
TrainingPage._ow_training_confirm_stop = _ow_training_confirm_stop
TrainingPage._ow_training_wait_for_cancel_slot = _ow_training_wait_for_cancel_slot
TrainingPage._ow_training_finish_pending_navigation = _ow_training_finish_pending_navigation
TrainingPage._ow_training_can_close = _ow_training_can_close
TrainingPage._ow_training_show_log = _ow_training_show_log
TrainingPage._ow_training_log_loaded = _ow_training_log_loaded
TrainingPage._ow_training_refresh_log = _ow_training_refresh_log
TrainingPage._ow_training_log_dialog_closed = _ow_training_log_dialog_closed
TrainingPage._ow_training_step_for_job = _ow_training_step_for_job
TrainingPage._ow_training_set_buttons = _ow_training_set_buttons
TrainingPage._ow_training_request_loaded = _ow_training_request_loaded
TrainingPage._ow_training_request_finished = _ow_training_request_finished
TrainingPage._ow_training_request_failed = _ow_training_request_failed
TrainingPage._ow_training_go_to_text = _ow_training_go_to_text
TrainingPage._ow_training_files_changed = _ow_training_files_changed
TrainingPage._ow_training_choose_avatar = _ow_training_choose_avatar
TrainingPage._ow_training_avatar_changed = _ow_training_avatar_changed
TrainingPage._ow_training_load_avatar = _ow_training_load_avatar
TrainingPage._ow_training_avatar_loaded = _ow_training_avatar_loaded
TrainingPage._ow_training_avatar_preview_finished = _ow_training_avatar_preview_finished
TrainingPage._ow_training_action_started = _ow_training_action_started
TrainingPage._ow_training_animate_status = _ow_training_animate_status
TrainingPage._ow_training_reset_preview_buttons = _ow_training_reset_preview_buttons
TrainingPage._ow_training_play_audio = _ow_training_play_audio
TrainingPage._ow_training_preview_status = _ow_training_preview_status
TrainingPage._ow_training_preview_error = _ow_training_preview_error
TrainingPage._ow_training_remove_audio = _ow_training_remove_audio
TrainingPage._ow_training_clear_audio = _ow_training_clear_audio
TrainingPage._ow_training_recent_job_loaded = _ow_training_recent_job_loaded
TrainingPage._ow_training_text_changed = _ow_training_text_changed
TrainingPage._ow_training_navigate = _ow_training_navigate
TrainingPage._ow_training_retry = _ow_training_retry
TrainingPage._text_saved = _ow_training_text_saved
TrainingPage._prepare = _ow_training_prepare
TrainingPage._start_prepare_request = _ow_training_start_prepare_request
TrainingPage._action_started = _ow_training_action_started
TrainingPage._transcribe = _ow_training_transcribe
TrainingPage._start_training = _ow_training_start_training
TrainingPage._ow_training_engine_released = _ow_training_engine_released
TrainingPage._finalize = _ow_training_finalize
TrainingPage._cancel = _ow_training_cancel
TrainingPage.restore_latest_job = _ow_training_restore_latest_job
TrainingPage.prepare_environment = _ow_training_prepare_environment
TrainingPage._ow_training_environment_loaded = _ow_training_environment_loaded
TrainingPage._ow_training_restore_latest_job = _ow_training_restore_latest_job
TrainingPage._ow_training_start_setup = _ow_training_start_setup
TrainingPage._ow_training_setup_output = _ow_training_setup_output
TrainingPage._ow_training_setup_success = _ow_training_setup_success
TrainingPage._ow_training_setup_failed = _ow_training_setup_failed
TrainingPage._ow_training_setup_finished = _ow_training_setup_finished


def _ow_open_training(self) -> None:
    # 训练页是独立工作区：隐藏导航，返回工作台时由 show_workspace 恢复。
    sidebar = self.tool_page.findChild(QFrame, "Sidebar") if hasattr(self, "tool_page") else None
    if sidebar is not None:
        sidebar.setVisible(False)
    self.workspace_stack.setCurrentWidget(self.training_page)
    self.training_page._set_buttons()
    self.training_page.prepare_environment()


_ow_app_base_init = OwVoiceApp.__init__
_ow_app_base_open_model_plaza = OwVoiceApp.open_model_plaza
_ow_app_base_show_workspace = OwVoiceApp.show_workspace


def _ow_app_init_with_training(self, *args, **kwargs):
    _ow_app_base_init(self, *args, **kwargs)
    self.training_page = TrainingPage(self.api_url(), self)
    self.training_page.back_requested.connect(self.show_workspace)
    # 保存本地训练模型后刷新角色，并复用模型导入后的自动启动逻辑。
    # 仅调用 load_voices 会留下“模型出现但引擎离线”的状态，重启后才恢复。
    self.training_page.model_saved.connect(self._on_models_changed)
    self.workspace_stack.addWidget(self.training_page)
    sidebar = self.tool_page.findChild(QFrame, "Sidebar") if hasattr(self, "tool_page") else None
    if sidebar is None or sidebar.layout() is None:
        return
    layout = sidebar.layout()
    layout.removeWidget(self.model_plaza_button)
    layout.removeWidget(self.about_button)
    self.model_plaza_button.setText("本地模型库")
    self.model_plaza_button.setFixedHeight(42)
    button = QPushButton("本地训练")
    button.setObjectName("TrainingButton")
    button.setFixedHeight(42)
    button.clicked.connect(lambda: _ow_open_training(self))
    button.setFocusPolicy(Qt.NoFocus)
    self.training_button = button
    layout.addWidget(self.model_plaza_button)
    layout.addWidget(button)
    layout.addWidget(self.about_button)


def _ow_open_model_plaza_full(self) -> None:
    if self.model_release_thread is not None:
        return
    if self._last_health and not self._last_health.get("active_voice_id") and not self._last_health.get("gpt_sovits_online"):
        _ow_model_release_success(self, False)
        return
    self.model_plaza_button.setEnabled(False)
    self.set_status("正在检查当前模型状态……", warning=True)
    self.model_release_thread = QThread(self)
    self.model_release_worker = ModelEngineUnloadWorker(self.api_url())
    self.model_release_worker.moveToThread(self.model_release_thread)
    self.model_release_thread.started.connect(self.model_release_worker.run)
    self.model_release_worker.success.connect(self._ow_model_release_success)
    self.model_release_worker.failed.connect(self._ow_model_release_failed)
    self.model_release_worker.finished.connect(self.model_release_thread.quit)
    self.model_release_worker.finished.connect(self.model_release_worker.deleteLater)
    self.model_release_thread.finished.connect(self._ow_model_release_finished)
    # model_release_thread 由主窗口关闭流程统一回收，避免卸载完成后的竞态删除。
    self.model_release_thread.start()


@Slot(bool)
def _ow_model_release_success(self, released: bool = True) -> None:
    self._last_health["active_voice_id"] = None
    sidebar = self.tool_page.findChild(QFrame, "Sidebar") if hasattr(self, "tool_page") else None
    if sidebar is not None:
        sidebar.setVisible(False)
    self.workspace_stack.setCurrentWidget(self.model_plaza_page)
    self.model_plaza_page.status_label.setText(
        "模型已释放，正在打开模型库……" if released else "当前没有已加载模型，正在打开模型库……"
    )
    self.model_plaza_page.refresh_catalog()


@Slot(str)
def _ow_model_release_failed(self, message: str) -> None:
    self.set_status(f"模型释放失败：{message}", error=True)
    QMessageBox.warning(self, "无法打开模型库", f"当前模型仍在使用，暂时无法进入模型库：\n{message}")


@Slot()
def _ow_model_release_finished(self) -> None:
    finished_thread = self.model_release_thread
    self.model_release_thread = None
    self.model_release_worker = None
    _ow_delete_finished_thread(finished_thread)
    self.model_plaza_button.setEnabled(True)


def _ow_show_workspace_with_sidebar(self) -> None:
    sidebar = self.tool_page.findChild(QFrame, "Sidebar") if hasattr(self, "tool_page") else None
    if sidebar is not None:
        sidebar.setVisible(True)
    self.workspace_stack.setCurrentWidget(self.workspace_page)
    if not self.voices:
        self.set_status("没有可用角色", error=True)
    elif self.engine_start_thread is not None:
        self.set_status("正在启动语音引擎……", warning=True)
    elif self._last_health.get("gpt_sovits_online"):
        self.set_status("引擎在线，尚未加载当前角色")
    else:
        self.set_status("GPT-SoVITS 未启动", warning=True)


OwVoiceApp.__init__ = _ow_app_init_with_training
OwVoiceApp.open_model_plaza = _ow_open_model_plaza_full
OwVoiceApp.show_workspace = _ow_show_workspace_with_sidebar
OwVoiceApp._ow_model_release_success = _ow_model_release_success
OwVoiceApp._ow_model_release_failed = _ow_model_release_failed
OwVoiceApp._ow_model_release_finished = _ow_model_release_finished
