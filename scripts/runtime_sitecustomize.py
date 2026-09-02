"""Activate OwVoice's optional GPU packages for new private-runtime processes."""

import os
import sys
from pathlib import Path


runtime_dir = Path(__file__).resolve().parent
gpu_site = runtime_dir / "gpu-site"
if (
    os.environ.get("OWVOICE_DISABLE_GPU") != "1"
    and (runtime_dir / "gpu.enabled").is_file()
    and gpu_site.is_dir()
):
    sys.path.insert(0, str(gpu_site))
