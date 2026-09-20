"""轻量日志关联标识与启动前轮换。"""

from __future__ import annotations

import os
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


_LOG_LOCK = threading.RLock()


def _release_identity(log_path: Path) -> tuple[str, str]:
    root = log_path.parent.parent
    version = "unknown"
    commit = "unknown"
    try:
        value = json.loads((root / "version.json").read_text(encoding="utf-8-sig"))
        version = str(value.get("version", version))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        value = json.loads((root / "BUILD_INFO.json").read_text(encoding="utf-8-sig"))
        commit = str(value.get("commit", value.get("git_commit", commit)))[:12]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return version, commit


def session_id() -> str:
    value = os.environ.get("OWVOICE_SESSION_ID", "").strip()
    if not value:
        value = uuid.uuid4().hex
        os.environ["OWVOICE_SESSION_ID"] = value
    return value[:16]


def new_error_reference(code: str) -> str:
    return f"{code} · {uuid.uuid4().hex[:8]}"


def rotate_log(path: str | Path, *, max_bytes: int, backup_count: int) -> bool:
    """只轮换指定文件；占用或权限失败时保持原状并返回 False。"""

    target = Path(path)
    if backup_count < 1 or not target.is_file() or target.stat().st_size < max_bytes:
        return True
    with _LOG_LOCK:
        try:
            oldest = target.with_name(f"{target.name}.{backup_count}")
            oldest.unlink(missing_ok=True)
            for index in range(backup_count - 1, 0, -1):
                source = target.with_name(f"{target.name}.{index}")
                if source.exists():
                    source.replace(target.with_name(f"{target.name}.{index + 1}"))
            target.replace(target.with_name(f"{target.name}.1"))
            return True
        except OSError:
            return False


def append_log(
    path: str | Path,
    message: str,
    *,
    module: str,
    level: str = "ERROR",
    error_reference: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(target, max_bytes=max_bytes, backup_count=backup_count)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()
        version, commit = _release_identity(target)
        reference = f" error_id={error_reference}" if error_reference else ""
        header = (
            f"[{now_utc.isoformat()} UTC | {now_local.isoformat()} local] "
            f"level={level} module={module} version={version} commit={commit} "
            f"session_id={session_id()}{reference}\n"
        )
        with _LOG_LOCK, target.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(header)
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass
