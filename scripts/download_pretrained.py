"""Download GPT-SoVITS inference assets and, optionally, local-training assets."""

from __future__ import annotations

import os
import shutil
import zipfile
import argparse
import time
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
TRAINING_REQUIRED_FILES = (
    TARGET_DIR / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
    TARGET_DIR / "v2Pro" / "s2Gv2Pro.pth",
    TARGET_DIR / "v2Pro" / "s2Dv2Pro.pth",
)
ALLOW_PATTERNS = [
    "s1v3.ckpt",
    "chinese-hubert-base/**",
    "chinese-roberta-wwm-ext-large/**",
    "fast_langdetect/lid.176.bin",
    "gsv-v4-pretrained/s2Gv4.pth",
    "gsv-v4-pretrained/vocoder.pth",
]
TRAINING_ALLOW_PATTERNS = [
    "sv/pretrained_eres2netv2w24s4ep4.ckpt",
    "v2Pro/s2Gv2Pro.pth",
    "v2Pro/s2Dv2Pro.pth",
]


def ready(*, training: bool = False) -> bool:
    required = REQUIRED_FILES + (TRAINING_REQUIRED_FILES if training else ())
    return all(path.is_file() for path in required)


def download_file(url: str, target: Path, *, attempts: int = 4) -> None:
    """可靠下载单个文件，避免网络中断留下伪完整目标文件。

    保留 ``.part`` 文件并优先使用 HTTP Range 续传；服务器不支持续传时
    自动从头下载。只有完整响应写入成功后才替换最终目标文件。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    print(f"Downloading {url}...")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        offset = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
                # 服务器拒绝续传，丢弃旧分片并在本轮从头写入。
                if offset and response.status_code == 200:
                    partial.unlink(missing_ok=True)
                    offset = 0
                elif offset and response.status_code == 416:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError("服务器拒绝当前下载分片，已准备重新下载")
                response.raise_for_status()
                if offset and response.status_code != 206:
                    raise RuntimeError(f"服务器未返回续传响应：HTTP {response.status_code}")

                expected = response.headers.get("Content-Length")
                expected_bytes = int(expected) if expected and expected.isdigit() else None
                mode = "ab" if offset else "wb"
                received = 0
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            received += len(chunk)
                if expected_bytes is not None and received != expected_bytes:
                    raise RuntimeError(
                        f"下载内容不完整：本次收到 {received} 字节，应为 {expected_bytes} 字节"
                    )
            partial.replace(target)
            return
        except (OSError, requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            print(f"Download interrupted (attempt {attempt}/{attempts}): {exc}; retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"下载失败：{url}\n{last_error}") from last_error


def ensure_zip_download(url: str, target: Path) -> None:
    """下载并验证 ZIP；已有损坏缓存会自动重新下载。"""

    if target.is_file():
        try:
            with zipfile.ZipFile(target) as package:
                if package.testzip() is not None:
                    raise zipfile.BadZipFile("ZIP 内部文件校验失败")
            return
        except (OSError, zipfile.BadZipFile):
            target.unlink(missing_ok=True)
    download_file(url, target)
    try:
        with zipfile.ZipFile(target) as package:
            if package.testzip() is not None:
                raise zipfile.BadZipFile("ZIP 内部文件校验失败")
    except (OSError, zipfile.BadZipFile) as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"下载的压缩包无效：{target}") from exc


def ensure_ffmpeg() -> None:
    ffmpeg = ENGINE_DIR / "ffmpeg.exe"
    ffprobe = ENGINE_DIR / "ffprobe.exe"
    if ffmpeg.is_file() and ffprobe.is_file():
        return
    archive = DOWNLOAD_DIR / "ffmpeg-essentials.zip"
    ensure_zip_download(FFMPEG_URL, archive)
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
    ensure_zip_download(G2PW_URL, archive)
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


def download(*, training: bool = False) -> None:
    ensure_ffmpeg()
    ensure_g2pw()
    if ready(training=training):
        scope = "training and inference" if training else "inference"
        print(f"GPT-SoVITS {scope} pretrained assets already exist.")
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
                "allow_patterns": ALLOW_PATTERNS + (TRAINING_ALLOW_PATTERNS if training else []),
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
    if not ready(training=training):
        required = REQUIRED_FILES + (TRAINING_REQUIRED_FILES if training else ())
        missing = [str(path.relative_to(TARGET_DIR)) for path in required if not path.is_file()]
        raise RuntimeError("Pretrained asset download incomplete: " + ", ".join(missing))
    scope = "training and inference" if training else "inference"
    print(f"GPT-SoVITS {scope} pretrained assets are ready.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--training",
        action="store_true",
        help="同时下载本地训练所需的预训练模型；普通首次配置不下载这些大文件",
    )
    args = parser.parse_args()
    download(training=args.training)
