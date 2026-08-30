"""OwVoice 可见启动入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_DIR = (
    Path(__file__).resolve().parents[1]
)
if getattr(sys, "frozen", False):
    executable_dir = Path(sys.executable).resolve().parent
    project_candidates = [executable_dir, *executable_dir.parents]
    PROJECT_DIR = next(
        (
            candidate
            for candidate in project_candidates
            if (candidate / "config").is_dir() and (candidate / "models").is_dir()
        ),
        executable_dir,
    )
os.environ.setdefault("OWVOICE_PROJECT_DIR", str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from frontend.startup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
