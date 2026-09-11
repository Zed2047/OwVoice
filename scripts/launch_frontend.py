"""OwVoice 可见启动入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_project_dir() -> Path:
    """发布版始终以 EXE 所在安装目录为准，不接受遗留环境变量劫持。"""

    source_project = Path(__file__).resolve().parents[1]
    if not getattr(sys, "frozen", False):
        return Path(os.environ.get("OWVOICE_PROJECT_DIR", source_project)).resolve()
    executable_dir = Path(sys.executable).resolve().parent
    project_candidates = [executable_dir, *executable_dir.parents]
    return next(
        (
            candidate
            for candidate in project_candidates
            if (
                (candidate / "config").is_dir()
                and (
                    (candidate / "data" / "models").is_dir()
                    or ((candidate / "assets").is_dir() and (candidate / "GPT-SoVITS").is_dir())
                )
            )
        ),
        executable_dir,
    )


PROJECT_DIR = resolve_project_dir()
if getattr(sys, "frozen", False):
    os.environ["OWVOICE_PROJECT_DIR"] = str(PROJECT_DIR)
else:
    os.environ.setdefault("OWVOICE_PROJECT_DIR", str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from frontend.startup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
