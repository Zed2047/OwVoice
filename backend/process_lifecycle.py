"""OwVoice 子进程的统一登记、停止与 Windows Job Object 管理。"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """把子进程绑定到前端生命周期；前端异常退出时系统负责清理。"""

    def __init__(self) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(handle)
            return
        self._kernel32 = kernel32
        self.handle = int(handle)

    def assign(self, process: subprocess.Popen) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process_handle)))

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


@dataclass
class _ManagedProcess:
    process: subprocess.Popen
    role: str
    created_at: float
    executable: str
    job_assigned: bool


def terminate_process_tree(pid: int, *, timeout: float = 5.0) -> None:
    """只终止指定 PID 的进程树，不按名称扫描或误杀其他 Python。"""

    try:
        parent = psutil.Process(pid)
        targets = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return
    for target in targets:
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(targets, timeout=timeout)
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=timeout)


class ProcessSupervisor:
    """管理由当前 OwVoice 前端创建的全部长期子进程。"""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve()
        self.state_path = self.project_dir / ".cache" / "runtime" / "processes.json"
        self.session_id = os.environ.get("OWVOICE_SESSION_ID", "")
        self._lock = threading.RLock()
        self._processes: dict[int, _ManagedProcess] = {}
        self._job = _WindowsJob()
        self._closed = False

    def register(self, process: subprocess.Popen, role: str) -> None:
        with self._lock:
            if self._closed:
                terminate_process_tree(process.pid)
                raise RuntimeError("OwVoice 正在关闭，不能再启动后台服务。")
            try:
                created_at = psutil.Process(process.pid).create_time()
            except psutil.Error:
                created_at = time.time()
            executable = str(process.args[0]) if isinstance(process.args, (list, tuple)) and process.args else ""
            managed = _ManagedProcess(
                process=process,
                role=role,
                created_at=created_at,
                executable=executable,
                job_assigned=self._job.assign(process),
            )
            self._processes[process.pid] = managed
            self._write_state()

    def stop_role(self, role: str) -> None:
        with self._lock:
            targets = [item for item in self._processes.values() if item.role == role]
        for item in targets:
            terminate_process_tree(item.process.pid)
        with self._lock:
            for item in targets:
                self._processes.pop(item.process.pid, None)
            self._write_state()

    def stop_pid(self, pid: int) -> None:
        with self._lock:
            managed = self._processes.get(pid)
        if managed is not None:
            terminate_process_tree(pid)
            with self._lock:
                self._processes.pop(pid, None)
                self._write_state()

    def stop_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            targets = list(self._processes.values())
            # Job Object 是异常退出保障；显式关闭时先交给 Windows 一次性终止。
            self._job.close()
        for item in targets:
            if item.process.poll() is None:
                terminate_process_tree(item.process.pid)
        with self._lock:
            self._processes.clear()
            self._remove_state()

    def _state(self) -> dict:
        alive = []
        for pid, item in list(self._processes.items()):
            if item.process.poll() is not None:
                self._processes.pop(pid, None)
                continue
            alive.append(
                {
                    "pid": pid,
                    "role": item.role,
                    "createdAt": item.created_at,
                    "executable": item.executable,
                    "jobAssigned": item.job_assigned,
                }
            )
        return {
            "schema": 1,
            "projectDir": str(self.project_dir),
            "ownerPid": os.getpid(),
            "sessionId": self.session_id,
            "processes": alive,
        }

    def _write_state(self) -> None:
        state = self._state()
        if not state["processes"]:
            self._remove_state()
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            # 登记文件只供更新器兜底；Job Object 和内存登记仍然有效。
            pass

    def _remove_state(self) -> None:
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass


_supervisors: dict[Path, ProcessSupervisor] = {}
_supervisors_lock = threading.Lock()


def get_process_supervisor(project_dir: Path) -> ProcessSupervisor:
    resolved = project_dir.resolve()
    with _supervisors_lock:
        supervisor = _supervisors.get(resolved)
        if supervisor is None or supervisor._closed:
            supervisor = ProcessSupervisor(resolved)
            _supervisors[resolved] = supervisor
        return supervisor
