"""OwVoice 单窗口启动流程：先显示加载页，再切换到正式工具界面。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests
import psutil
from PySide6.QtCore import QLockFile, QObject, QThread, Qt, Signal, Slot, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from frontend.app import ICON_PATH, OwVoiceApp, set_windows_app_identity
from backend.process_lifecycle import get_process_supervisor
from backend.logging_support import append_log, new_error_reference, rotate_log
from backend.error_reporting import report_user_error
from backend.text_encoding import read_text_tail


class StartupError(RuntimeError):
    """启动阶段的可读错误。"""


def install_frontend_exception_hook(project_dir: Path) -> None:
    """为无控制台的 EXE 保存未处理异常，便于定位闪退原因。"""
    log_path = project_dir / "logs" / "frontend.error.log"

    def handle(exc_type, exc_value, exc_traceback) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        append_log(
            log_path,
            "未处理的前端异常\n" + detail,
            module="frontend",
            error_reference=new_error_reference("STARTUP-500"),
        )

    sys.excepthook = handle


def install_qt_message_handler(project_dir: Path) -> None:
    """记录 Qt 原生警告，便于定位窗口或线程导致的 EXE 原生退出。"""
    log_path = project_dir / "logs" / "frontend.qt.log"

    def handle(mode, _context, message) -> None:
        mode_name = getattr(mode, "name", str(mode))
        append_log(log_path, f"{mode_name}: {message}", module="frontend-qt", level="WARNING")

    qInstallMessageHandler(handle)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_backend_belongs_to(
    project_dir: Path,
    *,
    attempts: int = 1,
    retry_delay: float = 0.5,
) -> None:
    """确认 8765 是当前 OwVoice 后端，并容忍后端刚启动时的短暂不可用。"""

    expected = project_dir.resolve()
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            # 后端健康接口还会探测 9880；无 GPT-SoVITS 时可能需要数秒才返回。
            response = requests.get("http://127.0.0.1:8765/api/health", timeout=5)
            response.raise_for_status()
            payload = response.json()
            running_project = payload.get("project_dir")
            if payload.get("owvoice") is not True or not running_project:
                raise StartupError(
                    "端口 8765 已被其他程序或旧版 OwVoice 占用。"
                    "请关闭其他 OwVoice/相关服务后重试。"
                )
            actual = Path(str(running_project)).expanduser().resolve()
            if actual != expected:
                raise StartupError(
                    f"端口 8765 正在使用另一套 OwVoice：{actual}\n"
                    f"当前安装目录：{expected}\n请关闭另一套 OwVoice 后重试。"
                )
            return
        except StartupError:
            raise
        except (OSError, ValueError, requests.RequestException) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(retry_delay)
                continue
            raise StartupError(
                "OwVoice 本地后端已打开端口，但健康检查暂时没有响应。"
                "请稍候重试；如果仍失败，请查看 logs\\backend.error.log。"
            ) from exc
    if last_error is not None:
        raise StartupError(
            "OwVoice 本地后端健康检查失败，请查看 logs\\backend.error.log。"
        ) from last_error
    raise StartupError("OwVoice 本地后端健康检查失败。")


def resolve_project_path(project_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else project_dir / path


def tail_file(path: Path, limit: int = 1800) -> str:
    return read_text_tail(path, limit)


class StartupRuntime:
    """持有服务和日志资源；生命周期独立于会被 Qt 删除的启动 worker。"""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve()
        self.process_supervisor = get_process_supervisor(self.project_dir)
        self.processes: list[subprocess.Popen] = []
        self.service_processes: dict[str, subprocess.Popen] = {}
        self.log_files: list[object] = []
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        process: subprocess.Popen,
        log_handles: tuple[object, object],
    ) -> None:
        with self._lock:
            self.processes.append(process)
            self.service_processes[name] = process
            self.log_files.extend(log_handles)
        self.process_supervisor.register(process, name)

    def stop_service(self, name: str) -> None:
        with self._lock:
            process = self.service_processes.pop(name, None)
        if process is not None and process.poll() is None:
            self.process_supervisor.stop_role(name)

    def stop_all(self) -> None:
        self.process_supervisor.stop_all()
        with self._lock:
            self.processes.clear()
            self.service_processes.clear()
            handles, self.log_files = self.log_files, []
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass


def warmup_gpt_sovits(project_dir: Path, voice: dict) -> None:
    """在普通 Python 线程中预热，不持有或调用任何 QObject。"""

    if os.environ.get("OWVOICE_WARMUP", "1").strip().lower() in {"0", "false", "off"}:
        return
    reference = resolve_project_path(project_dir, voice.get("reference_audio"))
    if reference is None or not reference.is_file():
        return
    inference = voice.get("inference") or {}
    params = {
        "text": "你好。",
        "text_language": str(voice.get("locale", "zh-CN")).split("-")[0],
        "speed": 1.0,
        "refer_wav_path": str(reference),
        "prompt_text": str(voice.get("prompt_text", "")),
        "prompt_language": voice.get("prompt_language", "zh"),
        "top_k": int(inference.get("top_k", 15)),
        "top_p": float(inference.get("top_p", 0.9)),
        "temperature": float(inference.get("temperature", 0.8)),
        "sample_steps": int(inference.get("sample_steps", 32)),
        "cut_punc": str(inference.get("cut_punc", "，。？！；：,.?!…")),
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post("http://127.0.0.1:9880/", json=params, timeout=180)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            return
        except Exception as exc:  # noqa: BLE001 - 预热失败不能影响主界面
            last_error = exc
            if attempt < 2:
                time.sleep(4)
    append_log(
        project_dir / "logs" / "warmup.error.log",
        f"后台预热失败：{last_error or '未知预热错误'}",
        module="engine",
        level="WARNING",
        error_reference=new_error_reference("ENGINE-202"),
    )


class StartupWorker(QObject):
    status = Signal(str)
    progress = Signal(int)
    ready = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, project_dir: Path, runtime: StartupRuntime | None = None) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.runtime = runtime or StartupRuntime(project_dir)
        self.processes = self.runtime.processes
        self.service_processes = self.runtime.service_processes
        self.log_files = self.runtime.log_files
        self.process_supervisor = self.runtime.process_supervisor
    def resolve_python_exe(self) -> Path:
        """OwVoice 使用项目目录下由用户安装的 Python 虚拟环境。"""

        candidate = self.project_dir / ".venv" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
        raise StartupError(
            "找不到 OwVoice 运行环境。请先运行 setup.bat。"
        )

    def resolve_nltk_data_dir(self) -> Path | None:
        """查找项目数据目录或虚拟环境中的 NLTK 资源。"""

        candidates = (
            self.project_dir / "data" / "nltk_data",
            self.project_dir / ".venv" / "nltk_data",
        )
        return next((candidate for candidate in candidates if candidate.is_dir()), None)

    def load_config(self) -> dict:
        config_path = self.project_dir / "config" / "voices.local.json"
        if not config_path.is_file():
            raise StartupError("找不到 config\\voices.local.json，请先完成首次配置。")
        try:
            from backend.server import load_voices
            voices = load_voices()
        except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise StartupError(f"读取人物配置失败：{exc}") from exc
        if not voices:
            raise StartupError("人物配置中没有启用的角色。")
        return {"voice": voices[0]}

    def start_process(
        self,
        name: str,
        command: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen:
        logs_dir = self.project_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / f"{name}.log"
        stderr_path = logs_dir / f"{name}.error.log"
        rotate_log(stdout_path, max_bytes=10 * 1024 * 1024, backup_count=3)
        rotate_log(stderr_path, max_bytes=10 * 1024 * 1024, backup_count=3)
        append_log(stdout_path, "服务日志开始。", module=name, level="INFO")
        append_log(stderr_path, "服务错误日志开始。", module=name, level="INFO")
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt":
            # EXE 使用无控制台模式时，子进程仍可能单独弹出控制台窗口。
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        environment = dict(env or os.environ)
        if name == "gpt_sovits":
            # 模型加载会产生短时 CPU/磁盘突发。让引擎从创建第一刻起低于
            # 前端优先级，避免 Windows 因 Qt 消息循环得不到调度而显示“未响应”。
            creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            environment["OMP_NUM_THREADS"] = "4"
            environment["MKL_NUM_THREADS"] = "4"
            environment["OPENBLAS_NUM_THREADS"] = "4"
            environment["NUMEXPR_NUM_THREADS"] = "4"
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        nltk_data_dir = self.resolve_nltk_data_dir()
        if nltk_data_dir is not None:
            environment["NLTK_DATA"] = str(nltk_data_dir)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=stdout,
                stderr=stderr,
                creationflags=creationflags,
                env=environment,
            )
        except OSError:
            stdout.close()
            stderr.close()
            raise
        try:
            self.runtime.register(name, process, (stdout, stderr))
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
            raise
        return process

    def stop_service(self, name: str) -> None:
        """停止由本次启动流程创建的单个服务，不影响 OwVoice 后端。"""

        self.runtime.stop_service(name)

    def wait_for_port(
        self,
        port: int,
        seconds: int,
        service_name: str,
        process=None,
        progress_start: int | None = None,
        progress_end: int | None = None,
    ) -> None:
        deadline = time.monotonic() + seconds
        started = time.monotonic()
        while time.monotonic() < deadline:
            if port_open(port):
                if progress_end is not None:
                    self.progress.emit(progress_end)
                return
            if progress_start is not None and progress_end is not None:
                phase_ratio = min((time.monotonic() - started) / 30.0, 0.95)
                self.progress.emit(progress_start + int((progress_end - progress_start) * phase_ratio))
            if process is not None and process.poll() is not None:
                log = tail_file(self.project_dir / "logs" / f"{service_name}.error.log")
                detail = f"{service_name} 启动后立即退出。"
                if log:
                    detail += f"\\n\\n日志尾部：\\n{log}"
                raise StartupError(detail)
            time.sleep(0.5)
        raise StartupError(f"等待 {service_name} 超时，请检查 logs 目录。")

    def start_gpt_sovits(self, voice: dict) -> None:
        if port_open(9880):
            raise StartupError(
                "端口 9880 已被其他 GPT-SoVITS/OwVoice 占用。"
                "请关闭其他 OwVoice 或 GPT-SoVITS 服务后重试。"
            )
        self.progress.emit(10)
        self.status.emit("正在启动 GPT-SoVITS 引擎……")
        python_exe = self.resolve_python_exe()
        gsv_root = Path(os.environ.get("OWVOICE_GSV_ROOT", self.project_dir / "GPT-SoVITS"))
        api_py = gsv_root / "api.py"
        gpt_path = resolve_project_path(self.project_dir, voice.get("gpt_model"))
        sovits_path = resolve_project_path(self.project_dir, voice.get("sovits_model"))
        ref_wav = resolve_project_path(self.project_dir, voice.get("reference_audio"))
        required = (python_exe, api_py, gpt_path, sovits_path, ref_wav)
        if any(path is None or not path.is_file() for path in required):
            missing = next(str(path) for path in required if path is None or not path.is_file())
            raise StartupError(f"启动 GPT-SoVITS 所需文件不存在：{missing}")
        nltk_data_dir = self.resolve_nltk_data_dir()
        if nltk_data_dir is None:
            raise StartupError("找不到 NLTK 语音处理资源，请先完成首次配置。")
        required_nltk = (
            nltk_data_dir / "taggers" / "averaged_perceptron_tagger_eng",
            nltk_data_dir / "corpora" / "cmudict",
        )
        missing_nltk = next((str(path) for path in required_nltk if not path.is_dir()), None)
        if missing_nltk:
            raise StartupError(
                "缺少 GPT-SoVITS 英文处理资源，请重新运行 setup.bat。"
                f"\n\n缺少：{missing_nltk}"
            )
        cut_punc = voice.get("inference", {}).get("cut_punc", "，。？！；：,.?!…")
        command = [
            str(python_exe), str(api_py), "-a", "127.0.0.1", "-p", "9880",
            "-s", str(sovits_path), "-g", str(gpt_path), "-dr", str(ref_wav),
            "-dt", str(voice.get("prompt_text", "")), "-dl",
            str(voice.get("prompt_language", "zh")), "-cp", str(cut_punc),
        ]
        process = self.start_process("gpt_sovits", command, gsv_root)
        self.wait_for_port(9880, 180, "GPT-SoVITS", process, progress_start=10, progress_end=70)
        self.status.emit("GPT-SoVITS 引擎已就绪。")

    def start_backend(self, voice: dict) -> None:
        if port_open(8765):
            ensure_backend_belongs_to(self.project_dir, attempts=4)
            self.progress.emit(90)
            self.status.emit("OwVoice 后端已在运行，正在复用……")
            return
        self.progress.emit(72)
        self.status.emit("正在启动 OwVoice 后端……")
        python_exe = self.resolve_python_exe()
        command = [
            str(python_exe), "-m", "uvicorn", "backend.server:app",
            "--host", "127.0.0.1", "--port", "8765",
        ]
        environment = os.environ.copy()
        environment["OWVOICE_INITIAL_VOICE_ID"] = str(voice.get("id", ""))
        process = self.start_process("backend", command, self.project_dir, environment)
        self.wait_for_port(8765, 30, "OwVoice 后端", process, progress_start=72, progress_end=88)
        ensure_backend_belongs_to(self.project_dir, attempts=20)
        deadline = time.monotonic() + 20
        started = time.monotonic()
        while time.monotonic() < deadline:
            try:
                if requests.get("http://127.0.0.1:8765/api/health", timeout=5).status_code == 200:
                    self.progress.emit(95)
                    return
            except requests.RequestException:
                pass
            self.progress.emit(88 + int(min((time.monotonic() - started) / 10.0, 0.7) * 7))
            time.sleep(0.5)
        raise StartupError("OwVoice 后端已打开端口，但健康检查未通过。")

    def warmup_gpt_sovits(self, voice: dict) -> None:
        """Warm up the active model in the background without delaying the main window."""
        warmup_gpt_sovits(self.project_dir, voice)

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(2)
            self.status.emit("正在检查 OwVoice 配置……")
            data = self.load_config()
            self.progress.emit(5)
            self.start_gpt_sovits(data["voice"])
            # GPT-SoVITS 已在启动参数中加载首个角色；后端通过环境变量同步状态，避免二次加载。
            self.start_backend(data["voice"])
            self.progress.emit(98)
            self.status.emit("模型加载完成，正在后台预热并打开 OwVoice……")
            threading.Thread(
                target=warmup_gpt_sovits,
                args=(self.project_dir, data["voice"]),
                name="owvoice-warmup",
                daemon=True,
            ).start()
            self.progress.emit(100)
            self.ready.emit()
        except Exception as exc:
            self.stop_services()
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def stop_services(self) -> None:
        self.runtime.stop_all()


class OwVoiceWindow(QMainWindow):
    """同一个窗口中的加载页和正式工具页。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OwVoice - 正在启动")
        self.resize(1040, 720)
        self.setMinimumSize(860, 620)
        self.loading_page = self._build_loading_page()
        self.setCentralWidget(self.loading_page)

    def _build_loading_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(
            "QWidget { background-color: #F5F4ED; color: #141413; }"
            "QLabel { background: transparent; }"
            "QPushButton { background-color: #C96442; color: #FAF9F5; border: none; border-radius: 8px; padding: 11px 18px; font-weight: 600; }"
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(96, 80, 96, 72)
        layout.setSpacing(18)
        kicker = QLabel("OWVOICE / STARTUP")
        kicker.setStyleSheet("color: #CC785C; font-family: Consolas; font-size: 12px; font-weight: bold;")
        layout.addWidget(kicker)
        title = QLabel("正在准备语音引擎")
        title.setStyleSheet("font-family: Georgia; font-size: 38px; font-weight: 600; color: #141413;")
        layout.addWidget(title)
        subtitle = QLabel("首次启动可能需要一些时间，请不要关闭此窗口。")
        subtitle.setStyleSheet("font-size: 15px; color: #5E5D59;")
        layout.addWidget(subtitle)
        self.status_label = QLabel("正在启动……")
        self.status_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #141413;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setStyleSheet("background: #FAF9F5; border: 1px solid #E8E6DC; border-radius: 10px; padding: 8px; color: #5E5D59;")
        layout.addWidget(self.details, 1)
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_selected = QPushButton("复制所选")
        copy_selected.clicked.connect(self._copy_selected_details)
        copy_all = QPushButton("复制全部")
        copy_all.clicked.connect(lambda: QApplication.clipboard().setText(self.details.toPlainText()))
        copy_row.addWidget(copy_selected)
        copy_row.addWidget(copy_all)
        layout.addLayout(copy_row)
        return page

    def _copy_selected_details(self) -> None:
        selected = self.details.textCursor().selectedText().replace("\u2029", "\n")
        if selected:
            QApplication.clipboard().setText(selected)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.details.append(message)

    def show_error(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status_label.setText("启动失败")
        self.status_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #C64545;")
        self.details.append(message)

    def show_main(self) -> None:
        old_page = self.centralWidget()
        main_window = OwVoiceApp()
        content = main_window.centralWidget()
        main_window.setCentralWidget(QWidget())
        self.setWindowTitle(main_window.windowTitle())
        self.setMinimumSize(main_window.minimumSize())
        self.resize(main_window.size())
        self.setCentralWidget(content)
        self._main_window = main_window
        old_page.deleteLater()


class StartupController(QObject):
    status = Signal(str)
    progress = Signal(int)
    ready = Signal()
    failed = Signal(str)

    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.startup_thread: QThread | None = None
        self.worker: StartupWorker | None = None
        self.runtime = StartupRuntime(project_dir)

    def start(self) -> None:
        self.startup_thread = QThread()
        self.worker = StartupWorker(self.project_dir, self.runtime)
        self.worker.moveToThread(self.startup_thread)
        # StartupWorker.run is replaced by the public-build compatibility layer
        # after the QObject subclass has been created.  PySide therefore cannot
        # rely on the class meta-object to queue that patched Python method to
        # the worker thread.  Force a direct call from QThread.started's emitter
        # thread; QThread emits started from the new worker thread, so all slow
        # model/backend startup work stays off the GUI event loop.
        self.startup_thread.started.connect(
            self.worker.run,
            Qt.ConnectionType.DirectConnection,
        )
        self.worker.status.connect(self.status)
        self.worker.progress.connect(self.progress)
        self.worker.ready.connect(self.ready)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.startup_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.startup_thread.finished.connect(self._startup_finished)
        # StartupController 持有线程到应用退出，再由统一关闭流程回收，
        # 避免启动完成后仍有引用访问已删除的 QThread。
        self.startup_thread.start()

    def _startup_finished(self) -> None:
        thread = self.startup_thread
        self.worker = None
        self.startup_thread = None
        if thread is not None:
            thread.deleteLater()

    def stop_service(self, name: str) -> None:
        self.runtime.stop_service(name)

    def stop_services(self) -> None:
        self.runtime.stop_all()


def prioritize_frontend_process() -> None:
    """提高 GUI 调度优先级，抵抗模型启动阶段的资源突发。"""

    if os.name != "nt":
        return
    try:
        psutil.Process(os.getpid()).nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except (psutil.Error, OSError, ValueError):
        # 优先级只是响应性保护，失败不能阻止程序启动。
        return


def main() -> int:
    set_windows_app_identity()
    prioritize_frontend_process()
    project_dir = Path(
        os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1])
    )
    # 每次启动生成新的本地前端会话，训练看门狗据此判断前端是否仍存活。
    os.environ["OWVOICE_SESSION_ID"] = uuid.uuid4().hex
    install_frontend_exception_hook(project_dir)
    app = QApplication(sys.argv)
    install_qt_message_handler(project_dir)
    lock_path = project_dir / ".cache" / "runtime" / "OwVoice.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_path))
    instance_lock.setStaleLockTime(30_000)
    if not instance_lock.tryLock(100):
        QMessageBox.warning(
            None,
            "OwVoice 已在运行",
            "检测到另一个 OwVoice 窗口正在运行。请先切换到已有窗口或关闭它后再试。",
        )
        return 1
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = OwVoiceApp(startup_mode=True)
    controller = StartupController(project_dir)
    window.startup_controller = controller
    controller.status.connect(window.set_startup_status)
    controller.progress.connect(window.set_startup_progress)
    controller.failed.connect(window.show_startup_error)
    controller.ready.connect(window.finish_startup)
    app.aboutToQuit.connect(controller.stop_services)
    window.show()
    window.start_startup_liveness_monitor()
    controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
# 公开版兼容层：没有内置角色模型时允许进入空工作台。
_ow_original_start_gpt_sovits = StartupWorker.start_gpt_sovits
_ow_original_start_backend = StartupWorker.start_backend
_ow_original_warmup = StartupWorker.warmup_gpt_sovits


def _ow_load_config(self) -> dict:
    config_path = self.project_dir / "config" / "voices.local.json"
    if not config_path.is_file():
        return {"voice": None}
    try:
        from backend.server import load_voices
        voices = load_voices()
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise StartupError(f"读取人物配置失败：{exc}") from exc
    return {"voice": voices[0] if voices else None}


def _ow_start_gpt_sovits(self, voice: dict | None) -> None:
    if voice is None:
        if port_open(9880):
            raise StartupError(
                "端口 9880 已被其他 GPT-SoVITS/OwVoice 占用。"
                "请关闭其他 OwVoice 或 GPT-SoVITS 服务后重试。"
            )
        self.progress.emit(70)
        self.status.emit("暂无本地模型，已跳过 GPT-SoVITS 启动。")
        return
    _ow_original_start_gpt_sovits(self, voice)


def _ow_start_backend(self, voice: dict | None) -> None:
    if voice is None:
        if port_open(8765):
            ensure_backend_belongs_to(self.project_dir, attempts=4)
            self.progress.emit(95)
            self.status.emit("OwVoice 后端已在运行，正在复用……")
            return
        self.progress.emit(72)
        self.status.emit("正在启动 OwVoice 本地后端……")
        python_exe = self.resolve_python_exe()
        command = [
            str(python_exe), "-m", "uvicorn", "backend.server:app",
            "--host", "127.0.0.1", "--port", "8765",
        ]
        environment = os.environ.copy()
        environment["OWVOICE_INITIAL_VOICE_ID"] = ""
        process = self.start_process("backend", command, self.project_dir, environment)
        self.wait_for_port(8765, 60, "OwVoice 后端", process, progress_start=72, progress_end=95)
        ensure_backend_belongs_to(self.project_dir, attempts=20)
        time.sleep(1.0)
        self.status.emit("OwVoice 本地后端已就绪。")
        return
    _ow_original_start_backend(self, voice)


def _ow_wait_model_catalog(self) -> None:
    """确认后端已经能够读取模型清单，再允许进入首页。"""

    ensure_backend_belongs_to(self.project_dir, attempts=4)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = requests.get("http://127.0.0.1:8765/api/models", timeout=3)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                self.progress.emit(98)
                self.status.emit(f"模型清单已就绪，共 {len(payload)} 个模型。")
                return
        except (OSError, ValueError, requests.RequestException):
            pass
        time.sleep(0.5)
    raise StartupError("模型清单加载超时，请检查 data\\models 和后端日志。")


def _ow_warmup(self, voice: dict | None) -> None:
    if voice is not None:
        _ow_original_warmup(self, voice)



def _ow_load_config_safe(self) -> dict:
    config_path = self.project_dir / "config" / "voices.local.json"
    if not config_path.is_file():
        return {"voice": None}
    try:
        from backend.server import load_voices, resolve_path
        voices = load_voices()
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise StartupError(f"读取人物配置失败：{exc}") from exc
    for voice in voices:
        required = (resolve_path(voice.get("gpt_model")), resolve_path(voice.get("sovits_model")), resolve_path(voice.get("reference_audio")))
        if all(path is not None and path.is_file() for path in required):
            return {"voice": voice}
    return {"voice": None}

@Slot()
def _ow_run_safe(self) -> None:
    try:
        self.progress.emit(2)
        self.status.emit("正在检查 OwVoice 配置……")
        data = self.load_config()
        voice = data.get("voice")
        if voice is None:
            # 即使没有语音模型，也要启动轻量 OwVoice 后端。
            # 本地模型库和更新按钮通过后端接口工作；GPT-SoVITS 引擎仍保持关闭。
            self.start_backend(None)
            self.wait_model_catalog()
            self.progress.emit(100)
            self.status.emit("未发现本地模型，已进入空工作台；可从本地模型库导入或开始训练。")
            self.ready.emit()
            return
        self.progress.emit(5)
        self.start_gpt_sovits(voice)
        self.start_backend(voice)
        self.wait_model_catalog()
        self.progress.emit(98)
        self.status.emit("模型加载完成，正在后台预热并打开 OwVoice……")
        threading.Thread(target=warmup_gpt_sovits, args=(self.project_dir, voice), name="owvoice-warmup", daemon=True).start()
        self.progress.emit(100)
        self.ready.emit()
    except Exception as exc:
        self.stop_services()
        self.failed.emit(
            report_user_error(
                self.project_dir,
                code="STARTUP-201",
                module="frontend",
                reason=str(exc),
                impact="OwVoice 本次未进入工作台，已启动的本实例服务正在清理。",
                advice="按提示关闭冲突程序或修复配置后重新启动。",
                exception=exc,
            )
        )
    finally:
        self.finished.emit()

StartupWorker.load_config = _ow_load_config_safe
StartupWorker.start_gpt_sovits = _ow_start_gpt_sovits
StartupWorker.start_backend = _ow_start_backend
StartupWorker.wait_model_catalog = _ow_wait_model_catalog
StartupWorker.warmup_gpt_sovits = _ow_warmup
StartupWorker.run = _ow_run_safe
