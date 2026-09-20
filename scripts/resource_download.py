"""共享的资源下载、校验和 ZIP 安全解压逻辑。"""

from __future__ import annotations

import hashlib
import http.client
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_ATTEMPTS = 4
DEFAULT_TIMEOUT = 120
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MEBIBYTE = 1024 * 1024


def format_megabytes(size: int) -> str:
    """把字节数转换为适合终端展示的 MiB 数值。"""

    return f"{max(0, int(size)) / MEBIBYTE:.1f} MB"


def _write_download_progress(current: int, total: int | None, *, complete: bool) -> None:
    if total is not None and total > 0:
        percent = min(100, int(current * 100 / total))
        message = (
            f"下载进度：{format_megabytes(current)} / "
            f"{format_megabytes(total)} ({percent}%)"
        )
    else:
        message = f"已下载：{format_megabytes(current)}"
    if sys.stdout.isatty():
        print("\r" + message, end="\n" if complete else "", flush=True)
    elif complete:
        # 输出被安装器重定向到日志时只保留最终汇总，避免日志刷满进度里程碑。
        print(message, flush=True)


def file_matches(
    target: Path,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> bool:
    """按大小和 SHA256 流式校验文件。"""

    if not target.is_file():
        return False
    if expected_size is not None and target.stat().st_size != expected_size:
        return False
    if expected_sha256 is None:
        return True
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected_sha256.lower()


def _response_status(response) -> int:
    return int(getattr(response, "status", getattr(response, "code", 200)))


def _header(response, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def download_file(
    url: str,
    target: Path,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    timeout: int | float = DEFAULT_TIMEOUT,
    opener: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """下载文件，支持 ``.part``、HTTP Range 续传和最终原子替换。

    网络中断时保留分片；服务端不支持 Range 时丢弃分片并从头开始。
    最终目标只有在大小和 SHA256 校验通过后才会出现或被替换。
    """

    if attempts < 1:
        raise ValueError("attempts 必须大于 0")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    opener = opener or urllib.request.urlopen
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        offset = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener(request, timeout=timeout) as response:
                status = _response_status(response)
                if offset and status in (200, 206):
                    if status == 200:
                        partial.unlink(missing_ok=True)
                        offset = 0
                    elif status == 206:
                        content_range = _header(response, "Content-Range") or ""
                        if not content_range.startswith(f"bytes {offset}-"):
                            raise RuntimeError("服务器返回了错误的 Range 起点")
                elif offset and status == 416:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError("服务器拒绝当前下载分片，已准备重新下载")
                elif status < 200 or status >= 300:
                    raise RuntimeError(f"下载源返回 HTTP {status}")

                content_length = _header(response, "Content-Length")
                expected_bytes = int(content_length) if content_length and content_length.isdigit() else None
                received = 0
                total_bytes = expected_size
                if total_bytes is None and expected_bytes is not None:
                    total_bytes = offset + expected_bytes
                last_reported = offset
                last_report_time = time.monotonic()
                progress_completed_reported = False
                mode = "ab" if offset else "wb"
                with partial.open(mode) as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        current = offset + received
                        now = time.monotonic()
                        completed = total_bytes is not None and current >= total_bytes
                        if completed or current - last_reported >= 16 * MEBIBYTE or now - last_report_time >= 2:
                            _write_download_progress(current, total_bytes, complete=completed)
                            progress_completed_reported = completed
                            last_reported = current
                            last_report_time = now
                if not progress_completed_reported:
                    _write_download_progress(offset + received, total_bytes, complete=True)
                if expected_bytes is not None and received != expected_bytes:
                    raise RuntimeError(
                        "下载内容不完整：本次收到 "
                        f"{format_megabytes(received)}，应为 {format_megabytes(expected_bytes)}"
                    )

            if expected_size is not None and partial.stat().st_size > expected_size:
                partial.unlink(missing_ok=True)
                raise RuntimeError("下载文件超过资源锁声明的大小")
            if not file_matches(partial, expected_size=expected_size, expected_sha256=expected_sha256):
                # 完整响应但校验不符时不能继续追加，否则会把坏内容越积越多。
                partial.unlink(missing_ok=True)
                raise RuntimeError("下载文件校验失败：文件大小或 SHA256 不匹配")
            partial.replace(target)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                partial.unlink(missing_ok=True)
            last_error = exc
            if attempt >= attempts:
                break
            sleep(min(2 ** (attempt - 1), 8))
        except (OSError, urllib.error.URLError, http.client.IncompleteRead, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"下载失败：{url}\n{last_error}") from last_error


def download_from_sources(
    sources: Iterable[str],
    target: Path,
    *,
    download_func: Callable = download_file,
    **kwargs,
) -> str:
    """按主源到备用源尝试下载，返回成功的源地址。"""

    last_error: Exception | None = None
    for source in sources:
        try:
            download_func(source, target, **kwargs)
            return source
        except Exception as exc:  # noqa: BLE001 - 每个来源失败后继续尝试备用源
            last_error = exc
    raise RuntimeError(f"所有资源来源均下载失败：{last_error}") from last_error


def ensure_zip_download(
    url: str,
    target: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    **kwargs,
) -> None:
    """下载并验证 ZIP；已有损坏缓存会自动删除并重新下载。"""

    target = Path(target)
    if target.is_file():
        try:
            if not file_matches(target, expected_size=expected_size, expected_sha256=expected_sha256):
                raise OSError("ZIP 大小或 SHA256 不匹配")
            with zipfile.ZipFile(target) as package:
                if package.testzip() is not None:
                    raise zipfile.BadZipFile("ZIP 内部文件校验失败")
            return
        except (OSError, zipfile.BadZipFile):
            target.unlink(missing_ok=True)
    download_file(url, target, expected_size=expected_size, expected_sha256=expected_sha256, **kwargs)
    try:
        with zipfile.ZipFile(target) as package:
            if package.testzip() is not None:
                raise zipfile.BadZipFile("ZIP 内部文件校验失败")
    except (OSError, zipfile.BadZipFile) as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"下载的压缩包无效：{target}") from exc


def _safe_member_path(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    if not normalized or "\x00" in normalized:
        raise RuntimeError(f"压缩包包含不安全路径：{filename}")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise RuntimeError(f"压缩包包含绝对路径：{filename}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise RuntimeError(f"压缩包包含目录穿越路径：{filename}")
    return "/".join(part.lower() for part in parts)


def safe_extract_all(package: zipfile.ZipFile, destination: Path) -> None:
    """拒绝绝对路径、目录穿越、链接、重复成员和超大解压体积。"""

    destination = Path(destination)
    root = destination.resolve()
    seen: set[str] = set()
    total_size = 0
    for info in package.infolist():
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise RuntimeError(f"压缩包包含不安全链接：{info.filename}")
        normalized = _safe_member_path(info.filename)
        if normalized in seen:
            raise RuntimeError(f"压缩包包含重复路径：{info.filename}")
        seen.add(normalized)
        total_size += int(info.file_size)
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("压缩包解压体积超过安全上限")
        output = (destination / info.filename.replace("\\", "/")).resolve()
        try:
            output.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"压缩包包含不安全路径：{info.filename}") from exc
    destination.mkdir(parents=True, exist_ok=True)
    package.extractall(destination)
