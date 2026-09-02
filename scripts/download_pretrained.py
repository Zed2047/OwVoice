"""Download only the GPT-SoVITS inference assets needed by OwVoice."""

from __future__ import annotations

import os
import hashlib
import shutil
import zipfile
from pathlib import Path

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
TARGET_DIR = PROJECT_DIR / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models"
ENGINE_DIR = PROJECT_DIR / "GPT-SoVITS"
TEXT_DIR = ENGINE_DIR / "GPT_SoVITS" / "text"
DOWNLOAD_DIR = PROJECT_DIR / ".cache" / "setup-downloads"
G2PW_URL = "https://www.modelscope.cn/models/kamiorinn/g2pw/resolve/master/G2PWModel_1.1.zip"
G2PW_SHA256 = "b116f6930a7ee55eef6576a8d8e14bf40c1106583439e8ae924b901512379c64"
PRETRAINED_REVISION = "336b2ec4e8d4ac74740798dd40af44e74659ecaf"
REQUIRED_FILES = (
    TARGET_DIR / "chinese-hubert-base" / "config.json",
    TARGET_DIR / "chinese-hubert-base" / "preprocessor_config.json",
    TARGET_DIR / "chinese-hubert-base" / "pytorch_model.bin",
    TARGET_DIR / "chinese-roberta-wwm-ext-large" / "config.json",
    TARGET_DIR / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin",
    TARGET_DIR / "chinese-roberta-wwm-ext-large" / "tokenizer.json",
)
ALLOW_PATTERNS = [
    "chinese-hubert-base/**",
    "chinese-roberta-wwm-ext-large/**",
]


def ready() -> bool:
    return all(path.is_file() for path in REQUIRED_FILES)


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}...")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_g2pw() -> None:
    model_dir = TEXT_DIR / "G2PWModel"
    if (model_dir / "g2pW.onnx").is_file():
        return
    archive = DOWNLOAD_DIR / "G2PWModel_1.1.zip"
    if not archive.is_file():
        download_file(G2PW_URL, archive)
    digest = sha256_file(archive)
    if digest != G2PW_SHA256:
        raise RuntimeError(f"G2PW archive SHA256 mismatch: {digest}")
    extract_dir = TEXT_DIR / "G2PWModel_1.1"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    print("Extracting G2PW model...")
    with zipfile.ZipFile(archive) as package:
        package.extractall(TEXT_DIR)
    if not model_dir.exists() and extract_dir.exists():
        extract_dir.rename(model_dir)
    if not (model_dir / "g2pW.onnx").is_file():
        raise RuntimeError("G2PW model extraction did not produce g2pW.onnx")


def download() -> None:
    ensure_g2pw()
    if ready():
        print("GPT-SoVITS pretrained assets already exist.")
        return

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is missing; GPT-SoVITS dependencies were not installed.") from exc

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    repo_id = os.environ.get("OWVOICE_PRETRAINED_REPO", "lj1995/GPT-SoVITS")
    print(f"Downloading GPT-SoVITS assets from {repo_id}...")
    endpoints = []
    if os.environ.get("HF_ENDPOINT"):
        endpoints.append(os.environ["HF_ENDPOINT"])
    endpoints.extend(["https://hf-mirror.com", None])
    last_error = None
    for endpoint in endpoints:
        try:
            kwargs = {
                "repo_id": repo_id,
                "revision": PRETRAINED_REVISION,
                "allow_patterns": ALLOW_PATTERNS,
                "local_dir": str(TARGET_DIR),
            }
            if endpoint:
                kwargs["endpoint"] = endpoint
            snapshot_download(**kwargs)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            print(f"Download endpoint failed: {endpoint or 'huggingface.co'}")
    if last_error is not None:
        raise last_error
    if not ready():
        missing = [str(path.relative_to(TARGET_DIR)) for path in REQUIRED_FILES if not path.is_file()]
        raise RuntimeError("Pretrained asset download incomplete: " + ", ".join(missing))
    print("GPT-SoVITS pretrained assets are ready.")


if __name__ == "__main__":
    download()
