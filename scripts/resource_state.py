"""资源运行状态的最小原子记录。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def update_resource_state(
    path: Path,
    resource_id: str,
    *,
    mode: str,
    reason: str = "",
    revision: str = "",
) -> None:
    """原子更新单个资源状态，不删除旧状态或用户数据。"""

    path = Path(path)
    state: dict[str, Any] = {"schema": 1, "resources": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("resources"), dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            # 状态损坏不阻断资源使用；新状态会覆盖状态文件本身。
            pass
    state["schema"] = 1
    state.setdefault("resources", {})[resource_id] = {
        "mode": mode,
        "reason": reason,
        "revision": revision,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
