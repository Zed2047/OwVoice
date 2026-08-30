"""Download only the GPT-SoVITS inference assets needed by OwVoice."""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
TARGET_DIR = PROJECT_DIR / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models"
ENGINE_DIR = PROJECT_DIR / "GPT-SoVITS"
TEXT_DIR = ENGINE_DIR / "GPT_SoVITS" / "text"
DOWNLOAD_DIR = PROJECT_DIR / ".cache" / "setup-downloads"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
G2PW_URL = "https://www.modelscope.cn/models/kamiorinn/g2pw/resolve/master/G2PWModel_1.1.zip"
REQUIRED_FILES = (
    TARGET_DIR / "s1v3.ckpt",
    TARGET_DIR / "chinese-hubert-base" / "config.json",
    TARGET_DIR / "chinese-roberta-wwm-ext-large" / "config.json",
    TARGET_DIR / "fast_langdetect" / "lid.176.bin",
    TARGET_DIR / "gsv-v4-pretrained" / "s2Gv4.pth",
    TARGET_DIR / "gsv-v4-pretrained" / "vocoder.pth",
)
ALLOW_PATTERNS = [
    "s1v3.ckpt",
    "chinese-hubert-base/**",
    "chinese-roberta-wwm-ext-large/**",
    "gsv-v4-pretrained/s2Gv4.pth",
    "gsv-v4-pretrained/vocoder.pth",
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


def ensure_ffmpeg() -> None:
    ffmpeg = ENGINE_DIR / "ffmpeg.exe"
    ffprobe = ENGINE_DIR / "ffprobe.exe"
    if ffmpeg.is_file() and ffprobe.is_file():
        return
    archive = DOWNLOAD_DIR / "ffmpeg-essentials.zip"
    if not archive.is_file():
        download_file(FFMPEG_URL, archive)
    print("Extracting ffmpeg...")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        for filename, destination in (("ffmpeg.exe", ffmpeg), ("ffprobe.exe", ffprobe)):
            matches = [name for name in names if name.endswith("/bin/" + filename)]
            if not matches:
                raise RuntimeError(f"ffmpeg archive does not contain {filename}")
            with package.open(matches[0]) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)


def ensure_g2pw() -> None:
    model_dir = TEXT_DIR / "G2PWModel"
    if (model_dir / "g2pW.onnx").is_file():
        return
    archive = DOWNLOAD_DIR / "G2PWModel_1.1.zip"
    if not archive.is_file():
        download_file(G2PW_URL, archive)
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
    ensure_ffmpeg()
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
                "revision": "main",
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
