"""跨 Windows 区域设置读取文本日志和用户元数据的辅助函数。"""

from __future__ import annotations

import locale
from pathlib import Path


def decode_text(data: bytes) -> str:
    """优先读取 UTF-8，并兼容旧中文 Windows 代码页生成的文本。"""

    encodings = ["utf-8-sig"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in {item.lower() for item in encodings}:
        encodings.append(preferred)
    if "gb18030" not in {item.lower() for item in encodings}:
        encodings.append("gb18030")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_text_tail(path: Path, limit: int = 1800) -> str:
    try:
        return decode_text(path.read_bytes())[-limit:].strip()
    except OSError:
        return ""


def read_text_compat(path: Path) -> str:
    """读取项目或用户提供的文本元数据，并兼容旧中文代码页。"""

    return decode_text(path.read_bytes())
