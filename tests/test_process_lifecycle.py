from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import psutil

from backend.process_lifecycle import ProcessSupervisor


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object 专用测试")
def test_job_object_kills_child_when_owner_exits(tmp_path: Path) -> None:
    helper = """
import os
import subprocess
import sys
from pathlib import Path
from backend.process_lifecycle import ProcessSupervisor

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
supervisor = ProcessSupervisor(Path(sys.argv[1]))
supervisor.register(child, "backend")
print(child.pid, flush=True)
os._exit(0)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", helper, str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert owner.stdout is not None
    child_pid = int(owner.stdout.readline().strip())
    owner.wait(timeout=10)
    try:
        deadline = time.monotonic() + 5
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not psutil.pid_exists(child_pid)
    finally:
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_supervisor_writes_instance_scoped_process_state(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path)
    supervisor._job = Mock()
    supervisor._job.assign.return_value = True
    process = Mock(spec=subprocess.Popen)
    process.pid = 1234
    process.args = [str(tmp_path / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn"]
    process.poll.return_value = None

    managed = Mock()
    managed.create_time.return_value = 123.5
    with patch("backend.process_lifecycle.psutil.Process", return_value=managed):
        supervisor.register(process, "backend")

    state = json.loads(supervisor.state_path.read_text(encoding="utf-8"))
    assert state["projectDir"] == str(tmp_path.resolve())
    assert state["ownerPid"] == os.getpid()
    assert state["processes"] == [
        {
            "pid": 1234,
            "role": "backend",
            "createdAt": 123.5,
            "executable": str(tmp_path / ".venv" / "Scripts" / "python.exe"),
            "jobAssigned": True,
        }
    ]


def test_supervisor_stops_only_requested_role(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path)
    supervisor._job = Mock()
    supervisor._job.assign.return_value = True
    backend = Mock(spec=subprocess.Popen, pid=1001, args=["python.exe"])
    engine = Mock(spec=subprocess.Popen, pid=1002, args=["python.exe"])
    backend.poll.return_value = None
    engine.poll.return_value = None

    with patch("backend.process_lifecycle.psutil.Process") as process_info:
        process_info.return_value.create_time.return_value = 1.0
        supervisor.register(backend, "backend")
        supervisor.register(engine, "gpt_sovits")
    with patch("backend.process_lifecycle.terminate_process_tree") as terminate:
        supervisor.stop_role("gpt_sovits")

    terminate.assert_called_once_with(1002)
    state = json.loads(supervisor.state_path.read_text(encoding="utf-8"))
    assert [item["pid"] for item in state["processes"]] == [1001]


@pytest.mark.skipif(os.name != "nt", reason="仅在 Windows 上验证 PowerShell 进程归属判断")
def test_powershell_process_match_is_limited_to_target_directory(tmp_path: Path) -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "process_lifecycle.ps1"
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    target = tmp_path / "OwVoice"
    other = tmp_path / "OwVoice-copy"
    command = f'''\
. "{script_path}"
$owned = [pscustomobject]@{{ Name="python.exe"; ExecutablePath="C:\\Python\\python.exe"; CommandLine='"{target}\\.venv\\Scripts\\python.exe" -m uvicorn' }}
$foreign = [pscustomobject]@{{ Name="python.exe"; ExecutablePath="C:\\Python\\python.exe"; CommandLine='"{other}\\.venv\\Scripts\\python.exe" -m uvicorn' }}
if (-not (Test-OwVoiceProcessBelongsToTarget $owned "{target}")) {{ exit 1 }}
if (Test-OwVoiceProcessBelongsToTarget $foreign "{target}") {{ exit 2 }}
'''
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_powershell_cleanup_kills_tree_roots_without_stale_child_errors() -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "process_lifecycle.ps1"
    source = script_path.read_text(encoding="utf-8-sig")
    assert "$roots" in source
    assert 'Start-Process -FilePath "taskkill.exe"' in source
    assert "& taskkill.exe" not in source
