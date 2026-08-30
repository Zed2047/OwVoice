"""Start the OwVoice backend from the project virtual environment."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from uvicorn import run  # noqa: E402


if __name__ == "__main__":
    run("backend.server:app", host="127.0.0.1", port=8765)
