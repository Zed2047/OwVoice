"""仅由用户主动触发的本地脱敏诊断报告。"""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import socket
import tempfile
import urllib.parse
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


LOG_TAIL_BYTES = 200_000
LOG_NAMES = {
    "frontend.error.log",
    "frontend.qt.log",
    "backend.log",
    "backend.error.log",
    "gpt_sovits.log",
    "gpt_sovits.error.log",
}


def _sanitize_url(match: re.Match[str]) -> str:
    value = match.group(0)
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def redact_text(
    text: str,
    *,
    project_root: Path,
    user_dir: Path | None = None,
    sensitive_values: list[str] | None = None,
) -> str:
    result = text.replace(str(project_root), "<OWVOICE_DIR>")
    if user_dir is not None:
        result = re.sub(re.escape(str(user_dir)), "<USER_DIR>", result, flags=re.IGNORECASE)
    result = re.sub(r"https?://[^\s\]\[\)\(<>]+", _sanitize_url, result)
    result = re.sub(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|cookie|password)\s*[:=]\s*[^\s]+",
        r"\1=<REDACTED>",
        result,
    )
    for value in sorted(set(sensitive_values or []), key=len, reverse=True):
        if len(value.strip()) < 3:
            continue
        marker = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        result = result.replace(value, f"<PRIVATE_{marker}>")
    return result


def _sensitive_values(root: Path) -> list[str]:
    values: list[str] = []
    paths = [*(root / "data" / "models").glob("*/model.json"), *(root / "data" / "training" / "jobs").glob("*/job.json")]
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("id", "name", "display_name", "model_id", "prompt_text", "text", "transcript"):
            if str(payload.get(key, "")).strip():
                values.append(str(payload[key]))
        for item in payload.get("files", []) if isinstance(payload.get("files"), list) else []:
            if isinstance(item, dict) and str(item.get("text", "")).strip():
                values.append(str(item["text"]))
    return values


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


def _model_summary(root: Path) -> dict[str, int]:
    valid = 0
    invalid = 0
    model_root = root / "data" / "models"
    if model_root.is_dir():
        for directory in model_root.iterdir():
            metadata = directory / "model.json"
            if not directory.is_dir() or not metadata.exists():
                continue
            try:
                value = json.loads(metadata.read_text(encoding="utf-8-sig"))
                valid += int(isinstance(value, dict))
                invalid += int(not isinstance(value, dict))
            except (OSError, UnicodeError, json.JSONDecodeError):
                invalid += 1
    return {"valid": valid, "invalid": invalid}


def _training_summary(root: Path) -> dict[str, int]:
    statuses: Counter[str] = Counter()
    jobs_root = root / "data" / "training" / "jobs"
    if jobs_root.is_dir():
        for path in jobs_root.glob("*/job.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                status = str(value.get("status", "invalid")) if isinstance(value, dict) else "invalid"
            except (OSError, UnicodeError, json.JSONDecodeError):
                status = "invalid"
            statuses[status] += 1
    return dict(sorted(statuses.items()))


def _log_candidates(root: Path) -> list[Path]:
    logs = root / "logs"
    if not logs.is_dir():
        return []
    fixed = [logs / name for name in sorted(LOG_NAMES)]
    per_run = sorted(
        [*logs.glob("setup-*.log"), *logs.glob("update-*.log")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:4]
    return [path for path in [*fixed, *per_run] if path.is_file()]


def create_diagnostic_report(project_dir: str | Path, *, version: str) -> Path:
    root = Path(project_dir).resolve()
    output_dir = root / "logs" / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / (
        f"OwVoice-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    )
    descriptor, temporary_name = tempfile.mkstemp(dir=output_dir, prefix=".diagnostics-", suffix=".tmp")
    os.close(descriptor)
    temporary: Path | None = Path(temporary_name)
    included_logs: list[dict[str, Any]] = []
    user_dir = Path.home()
    sensitive_values = _sensitive_values(root)
    try:
        summary: dict[str, Any] = {
            "schema": 1,
            "version": version,
            "system": {"windows": platform.platform(), "architecture": platform.machine()},
            "disk_free_bytes": shutil.disk_usage(root).free,
            "ports": {"8765": _port_open(8765), "9880": _port_open(9880)},
            "models": _model_summary(root),
            "training_status_counts": _training_summary(root),
            "logs": included_logs,
            "privacy": "No model weights, audio, avatars, prompts, transcripts, credentials, or environment dump included.",
        }
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "README.txt",
                "OwVoice 本地诊断报告。报告已默认脱敏且不会自动上传；分享前请自行复查。\n",
            )
            log_payloads: list[tuple[str, bytes]] = []
            for path in _log_candidates(root):
                data = path.read_bytes()[-LOG_TAIL_BYTES:]
                text = data.decode("utf-8", errors="replace")
                sanitized = redact_text(
                    text,
                    project_root=root,
                    user_dir=user_dir,
                    sensitive_values=sensitive_values,
                ).encode("utf-8")
                if len(sanitized) > LOG_TAIL_BYTES:
                    sanitized = sanitized[-LOG_TAIL_BYTES:].decode("utf-8", errors="ignore").encode("utf-8")
                relative = f"logs/{path.name}"
                log_payloads.append((relative, sanitized))
                included_logs.append({"file": path.name, "bytes_included": len(sanitized)})
            archive.writestr(
                "diagnostic-summary.json",
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            )
            for name, data in log_payloads:
                archive.writestr(name, data)
        os.replace(temporary, report)
        temporary = None
        reports = sorted(output_dir.glob("OwVoice-diagnostics-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in reports[3:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return report
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
