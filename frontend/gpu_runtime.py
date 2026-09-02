"""Optional NVIDIA runtime download and activation."""

from __future__ import annotations

import csv
import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Callable, NamedTuple


GIB = 1024**3
GPU_COMPONENTS = {
    "cu126": {
        "id": "cp310-torch-2.7.0+cu126",
        "torch_version": "2.7.0+cu126",
        "minimum_driver": "560.76",
        "required_free_bytes": 7 * GIB,
        "max_extract_bytes": 6 * GIB,
        "wheels": (
            (
                "torch-2.7.0+cu126-cp310-cp310-win_amd64.whl",
                "https://download-r2.pytorch.org/whl/cu126/torch-2.7.0%2Bcu126-cp310-cp310-win_amd64.whl",
                2_770_884_935,
                "587dec2f6c9e3316faea05f22434a386d402cf02d6faeb97a8978f73b3a0ed7a",
            ),
            (
                "torchaudio-2.7.0+cu126-cp310-cp310-win_amd64.whl",
                "https://download-r2.pytorch.org/whl/cu126/torchaudio-2.7.0%2Bcu126-cp310-cp310-win_amd64.whl",
                4_218_082,
                "5e860908eaf6364d26d7844b7af9ad2d26c3dfe1cc2281f94cadddae8f6d0328",
            ),
        ),
    },
    "cu128": {
        "id": "cp310-torch-2.7.0+cu128",
        "torch_version": "2.7.0+cu128",
        "minimum_driver": "570.65",
        "required_free_bytes": 9 * GIB,
        "max_extract_bytes": 7 * GIB,
        "wheels": (
            (
                "torch-2.7.0+cu128-cp310-cp310-win_amd64.whl",
                "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp310-cp310-win_amd64.whl",
                3_338_322_003,
                "c52c4b869742f00b12cb34521d1381be6119fa46244791704b00cc4a3cb06850",
            ),
            (
                "torchaudio-2.7.0+cu128-cp310-cp310-win_amd64.whl",
                "https://download-r2.pytorch.org/whl/cu128/torchaudio-2.7.0%2Bcu128-cp310-cp310-win_amd64.whl",
                4_653_922,
                "f96c2be8aff6c827e76fd3a85e69a54ba5b9a37090853ed886f056ddfbca09a4",
            ),
        ),
    },
}


class NvidiaGpu(NamedTuple):
    name: str
    driver: str
    component: str


class GpuRuntimeError(RuntimeError):
    pass


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _nvidia_smi() -> str | None:
    executable = shutil.which("nvidia-smi")
    if executable:
        return executable
    windows_dir = os.environ.get("WINDIR")
    if windows_dir:
        candidate = Path(windows_dir) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _uses_cu128(name: str, compute_capability: str) -> bool:
    try:
        if int(float(compute_capability)) >= 10:
            return True
    except (TypeError, ValueError):
        pass
    return bool(re.search(r"RTX\s*50|B100|B200|GB10|Blackwell", name, re.IGNORECASE))


def detect_nvidia_gpu() -> NvidiaGpu | None:
    """Return the installed NVIDIA GPU and the smallest compatible wheel family."""
    executable = _nvidia_smi()
    if not executable:
        return None
    queries = (
        ("name,driver_version,compute_cap", 3),
        ("name,driver_version", 2),
    )
    for query, columns in queries:
        try:
            result = subprocess.run(
                [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=8,
                creationflags=_creation_flags(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            continue
        rows = [row for row in csv.reader(result.stdout.splitlines()) if len(row) >= columns]
        if not rows:
            continue
        names = [row[0].strip() for row in rows]
        drivers = [row[1].strip() for row in rows]
        use_cu128 = any(
            _uses_cu128(row[0], row[2] if columns == 3 else "") for row in rows
        )
        return NvidiaGpu(
            " / ".join(dict.fromkeys(names)),
            " / ".join(dict.fromkeys(drivers)),
            "cu128" if use_cu128 else "cu126",
        )
    return None


def component_download_bytes(component: str) -> int:
    return sum(wheel[2] for wheel in GPU_COMPONENTS[component]["wheels"])


def driver_is_supported(gpu: NvidiaGpu) -> bool:
    required = tuple(int(part) for part in GPU_COMPONENTS[gpu.component]["minimum_driver"].split("."))
    versions = re.findall(r"\d+(?:\.\d+)+", gpu.driver)
    if not versions:
        return True
    return all(tuple(int(part) for part in version.split(".")[:2]) >= required for version in versions)


def active_gpu_component(project_dir: Path) -> str | None:
    runtime_dir = project_dir / "runtime"
    marker = runtime_dir / "gpu.enabled"
    if not marker.is_file() or not (runtime_dir / "gpu-site").is_dir():
        return None
    try:
        component_id = marker.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return next(
        (key for key, value in GPU_COMPONENTS.items() if value["id"] == component_id),
        None,
    )


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise GpuRuntimeError("GPU 组件安装已取消。")


def _sha256(path: Path, cancel_event: Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            _check_cancelled(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def _download_wheel(
    wheel: tuple[str, str, int, str],
    cache_dir: Path,
    completed_bytes: int,
    total_bytes: int,
    report: Callable[[str], None],
    cancel_event: Event | None,
) -> Path:
    filename, url, expected_size, expected_sha256 = wheel
    final_path = cache_dir / filename
    part_path = cache_dir / f"{filename}.part"
    if final_path.is_file():
        if final_path.stat().st_size == expected_size and _sha256(final_path, cancel_event) == expected_sha256:
            return final_path
        final_path.unlink()
    if part_path.is_file() and part_path.stat().st_size > expected_size:
        part_path.unlink()
    if part_path.is_file() and part_path.stat().st_size == expected_size:
        if _sha256(part_path, cancel_event) == expected_sha256:
            os.replace(part_path, final_path)
            return final_path
        part_path.unlink()

    existing = part_path.stat().st_size if part_path.is_file() else 0
    headers = {"User-Agent": "OwVoice GPU runtime installer"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except OSError as exc:
        raise GpuRuntimeError(f"下载 {filename} 失败：{exc}") from exc

    with response:
        status = response.getcode()
        mode = "ab"
        if existing and status == 206:
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if (
                not match
                or int(match.group(1)) != existing
                or int(match.group(2)) != expected_size - 1
                or int(match.group(3)) != expected_size
            ):
                raise GpuRuntimeError(f"{filename} 的断点响应无效。")
        elif status == 200:
            existing = 0
            mode = "wb"
        else:
            raise GpuRuntimeError(f"下载 {filename} 失败：HTTP {status}")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) != expected_size - existing:
            raise GpuRuntimeError(f"{filename} 的响应长度无效。")

        last_bucket = -1
        oversized = False
        with part_path.open(mode) as target:
            while chunk := response.read(8 * 1024 * 1024):
                _check_cancelled(cancel_event)
                if existing + len(chunk) > expected_size:
                    oversized = True
                    break
                target.write(chunk)
                existing += len(chunk)
                bucket = min(10, int((completed_bytes + existing) * 10 / total_bytes))
                if bucket != last_bucket:
                    report(f"正在下载 NVIDIA GPU 组件……{bucket * 10}%")
                    last_bucket = bucket
        if oversized:
            part_path.unlink(missing_ok=True)
            raise GpuRuntimeError(f"{filename} 的响应超过固定大小。")

    if part_path.stat().st_size != expected_size:
        raise GpuRuntimeError(
            f"{filename} 下载不完整：{part_path.stat().st_size} / {expected_size} 字节。"
        )
    report(f"正在校验 {filename}……")
    if _sha256(part_path, cancel_event) != expected_sha256:
        part_path.unlink(missing_ok=True)
        raise GpuRuntimeError(f"{filename} 的 SHA256 校验失败。")
    os.replace(part_path, final_path)
    return final_path


def _safe_wheel_members(archive: zipfile.ZipFile, max_extract_bytes: int):
    members = []
    total = 0
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
        ):
            raise GpuRuntimeError(f"GPU wheel 包含不安全路径：{name!r}")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == 0o120000:
            raise GpuRuntimeError(f"GPU wheel 包含不支持的符号链接：{name!r}")
        lower_parts = tuple(part.lower() for part in path.parts)
        if name.lower().endswith(".lib") or lower_parts[:2] == ("torch", "include"):
            continue
        total += info.file_size
        if total > max_extract_bytes:
            raise GpuRuntimeError("GPU wheel 展开体积超过安全上限。")
        members.append((info, path.parts))
    return members


def _extract_wheels(
    wheels: list[Path],
    staging_dir: Path,
    max_extract_bytes: int,
    report: Callable[[str], None],
    cancel_event: Event | None,
) -> None:
    for wheel_path in wheels:
        report(f"正在安装 {wheel_path.name}……")
        try:
            with zipfile.ZipFile(wheel_path) as archive:
                for info, parts in _safe_wheel_members(archive, max_extract_bytes):
                    _check_cancelled(cancel_event)
                    target = staging_dir.joinpath(*parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as destination:
                        while chunk := source.read(8 * 1024 * 1024):
                            _check_cancelled(cancel_event)
                            destination.write(chunk)
        except (OSError, zipfile.BadZipFile) as exc:
            raise GpuRuntimeError(f"解压 {wheel_path.name} 失败：{exc}") from exc


_GPU_PROBE = """
import sys
site, expected_torch, expected_audio = sys.argv[1:4]
sys.path.insert(0, site)
import torch
import torchaudio
assert torch.__version__ == expected_torch, torch.__version__
assert torchaudio.__version__ == expected_audio, torchaudio.__version__
assert torch.cuda.is_available(), "CUDA unavailable"
for index in range(torch.cuda.device_count()):
    major, minor = torch.cuda.get_device_capability(index)
    memory_gb = torch.cuda.get_device_properties(index).total_memory / 1024**3 + 0.4
    if major + minor / 10 >= 5.3 and memory_gb >= 4:
        assert torch.ones(1, device=f"cuda:{index}").item() == 1
        print(torch.cuda.get_device_name(index))
        break
else:
    raise RuntimeError("没有满足 OwVoice 要求的 NVIDIA GPU（至少约 4 GB 显存）。")
"""


def probe_gpu_component(
    python_exe: Path,
    gpu_site: Path,
    component: str,
    cancel_event: Event | None = None,
) -> str:
    spec = GPU_COMPONENTS[component]
    environment = os.environ.copy()
    environment["OWVOICE_DISABLE_GPU"] = "1"
    process = subprocess.Popen(
        [
            str(python_exe),
            "-c",
            _GPU_PROBE,
            str(gpu_site),
            spec["torch_version"],
            spec["torch_version"],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=_creation_flags(),
        env=environment,
    )
    deadline = time.monotonic() + 180
    while process.poll() is None:
        if cancel_event is not None and cancel_event.wait(0.2):
            process.kill()
            process.communicate()
            raise GpuRuntimeError("GPU 运行检查已取消。")
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate()
            raise GpuRuntimeError("NVIDIA GPU 运行检查超时。")
        if cancel_event is None:
            time.sleep(0.2)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        detail = (stderr or stdout).strip()[-1200:]
        raise GpuRuntimeError(f"NVIDIA GPU 运行检查失败：{detail or '未知错误'}")
    return next((line.strip() for line in reversed(stdout.splitlines()) if line.strip()), "NVIDIA GPU")


def _acquire_install_mutex():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, "Local\\OwVoiceGpuRuntimeInstall")
    if not handle:
        raise GpuRuntimeError("无法创建 GPU 组件安装锁。")
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        raise GpuRuntimeError("另一个 OwVoice 实例正在安装 GPU 组件。")
    return close_handle, handle


def install_gpu_component(
    project_dir: Path,
    python_exe: Path,
    component: str,
    report: Callable[[str], None],
    cancel_event: Event | None = None,
) -> str:
    """Download, verify, probe, and atomically activate one CUDA runtime."""
    if component not in GPU_COMPONENTS:
        raise GpuRuntimeError(f"未知 GPU 组件：{component}")
    runtime_dir = project_dir / "runtime"
    if not runtime_dir.is_dir() or not python_exe.is_file():
        raise GpuRuntimeError("只支持为已安装的 OwVoice 添加 GPU 组件。")

    mutex = _acquire_install_mutex()
    spec = GPU_COMPONENTS[component]
    gpu_site = runtime_dir / "gpu-site"
    marker = runtime_dir / "gpu.enabled"
    cache_root = runtime_dir / "gpu-downloads"
    cache_dir = cache_root / component
    staging_dir = runtime_dir / f".gpu-site-{os.getpid()}.tmp"
    try:
        marker.unlink(missing_ok=True)
        for old_staging in runtime_dir.glob(".gpu-site-*.tmp"):
            shutil.rmtree(old_staging, ignore_errors=True)
        if gpu_site.exists():
            shutil.rmtree(gpu_site)
        required_free = spec["required_free_bytes"]
        free = shutil.disk_usage(runtime_dir).free
        if free < required_free:
            raise GpuRuntimeError(
                f"磁盘空间不足：GPU 组件安装需要至少 {required_free / GIB:.0f} GB 可用空间。"
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir(parents=True)
        total_download = component_download_bytes(component)
        wheel_paths = []
        completed = 0
        for wheel in spec["wheels"]:
            _check_cancelled(cancel_event)
            wheel_paths.append(
                _download_wheel(
                    wheel,
                    cache_dir,
                    completed,
                    total_download,
                    report,
                    cancel_event,
                )
            )
            completed += wheel[2]

        _extract_wheels(
            wheel_paths,
            staging_dir,
            spec["max_extract_bytes"],
            report,
            cancel_event,
        )
        _check_cancelled(cancel_event)
        report("正在验证 NVIDIA GPU 加速……")
        gpu_name = probe_gpu_component(python_exe, staging_dir, component, cancel_event)
        os.replace(staging_dir, gpu_site)
        marker_temp = runtime_dir / "gpu.enabled.tmp"
        marker_temp.write_text(spec["id"] + "\n", encoding="ascii")
        os.replace(marker_temp, marker)
        shutil.rmtree(cache_root, ignore_errors=True)
        return gpu_name
    except Exception:
        marker.unlink(missing_ok=True)
        if cancel_event is None or not cancel_event.is_set():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        if mutex is not None:
            close_handle, handle = mutex
            close_handle(handle)
