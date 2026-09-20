"""面向用户的简短错误摘要与日志关联。"""

from __future__ import annotations

import traceback
from pathlib import Path

from backend.logging_support import append_log, new_error_reference


def report_user_error(
    project_dir: str | Path,
    *,
    code: str,
    module: str,
    reason: str,
    impact: str,
    advice: str,
    exception: BaseException | None = None,
) -> str:
    reference = new_error_reference(code)
    detail = reason
    if exception is not None:
        detail += "\n" + "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
    append_log(
        Path(project_dir) / "logs" / f"{module}.error.log",
        detail,
        module=module,
        error_reference=reference,
    )
    return f"原因：{reason}\n影响：{impact}\n建议：{advice}\n错误编号：{reference}"
