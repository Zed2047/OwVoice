"""Download GPT-SoVITS inference assets and, optionally, local-training assets."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
import argparse
import time
import hashlib
from pathlib import Path

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
TARGET_DIR = PROJECT_DIR / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models"
ENGINE_DIR = PROJECT_DIR / "GPT-SoVITS"
TEXT_DIR = ENGINE_DIR / "GPT_SoVITS" / "text"
DOWNLOAD_DIR = PROJECT_DIR / ".cache" / "setup-downloads"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
G2PW_URL = "https://www.modelscope.cn/models/kamiorinn/g2pw/resolve/master/G2PWModel_1.1.zip"
G2PW_ONNX_SIZE = 635212732
G2PW_ONNX_SHA256 = "2eb3c71fd95117b2e1abef8d2d0cd78aae894bbe7f0fac105ddc9c32ce63cbd0"
DEFAULT_PRETRAINED_REPO = "lj1995/GPT-SoVITS"
DEFAULT_PRETRAINED_REVISION = "336b2ec4e8d4ac74740798dd40af44e74659ecaf"
FASTTEXT_LID_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
FASTTEXT_LID_SIZE = 131266198
FASTTEXT_LID_SHA256 = "7e69ec5451bc261cc7844e49e4792a85d7f09c06789ec800fc4a44aec362764e"
REQUIRED_FILES = (
    TARGET_DIR / "s1v3.ckpt",
    TARGET_DIR / "chinese-hubert-base" / "config.json",
    TARGET_DIR / "chinese-roberta-wwm-ext-large" / "config.json",
    TARGET_DIR / "gsv-v4-pretrained" / "s2Gv4.pth",
    TARGET_DIR / "gsv-v4-pretrained" / "vocoder.pth",
)
REQUIRED_FILE_CHECKS = {
    TARGET_DIR / "s1v3.ckpt": (155284856, "87133414860ea14ff6620c483a3db5ed07b44be42e2c3fcdad65523a729a745a"),
    TARGET_DIR / "gsv-v4-pretrained" / "s2Gv4.pth": (769025545, "906fe22f48c3e037a389df291d4d32a9414e15dbb8f9628643e83aaced109ea4"),
    TARGET_DIR / "gsv-v4-pretrained" / "vocoder.pth": (57781109, "4d611913df7b12d49e8976c944558d2d096816365edfc6c35a9e85b67dd14ed9"),
}
TRAINING_REQUIRED_FILES = (
    TARGET_DIR / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
    TARGET_DIR / "v2Pro" / "s2Gv2Pro.pth",
    TARGET_DIR / "v2Pro" / "s2Dv2Pro.pth",
)
ALLOW_PATTERNS = [
    "s1v3.ckpt",
    "chinese-hubert-base/**",
    "chinese-roberta-wwm-ext-large/**",
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
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if path in REQUIRED_FILE_CHECKS:
            size, sha256 = REQUIRED_FILE_CHECKS[path]
            if not file_matches(path, expected_size=size, expected_sha256=sha256):
                return False
    return True


def file_matches(target: Path, *, expected_size: int | None = None, expected_sha256: str | None = None) -> bool:
    if not target.is_file():
        return False
    if expected_size is not None and target.stat().st_size != expected_size:
        return False
    if expected_sha256 is not None:
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower() == expected_sha256.lower()
    return True


def download_file(
    url: str,
    target: Path,
    *,
    attempts: int = 4,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> None:
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
            if not file_matches(partial, expected_size=expected_size, expected_sha256=expected_sha256):
                raise RuntimeError("下载文件校验失败：文件大小或 SHA256 不匹配")
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


def safe_extract_all(package: zipfile.ZipFile, destination: Path) -> None:
    """拒绝绝对路径和目录穿越条目，再解压到指定临时目录。"""

    root = destination.resolve()
    for info in package.infolist():
        output = (destination / info.filename).resolve()
        try:
            output.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"压缩包包含不安全路径：{info.filename}") from exc
    package.extractall(destination)


def ffmpeg_works(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 50 * 1024 * 1024:
        return False
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.startswith("ffmpeg version")


def ensure_ffmpeg() -> None:
    ffmpeg = ENGINE_DIR / "ffmpeg.exe"
    ffprobe = ENGINE_DIR / "ffprobe.exe"
    if ffmpeg_works(ffmpeg) and ffprobe.is_file() and ffprobe.stat().st_size >= 50 * 1024 * 1024:
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
            temporary = destination.with_name(destination.name + ".new")
            with package.open(matches[0]) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            temporary.replace(destination)
    if not ffmpeg_works(ffmpeg):
        raise RuntimeError("ffmpeg 解压完成但无法运行；压缩包可能损坏或不兼容当前 Windows。")


def ensure_g2pw() -> None:
    model_dir = TEXT_DIR / "G2PWModel"
    model_file = model_dir / "g2pW.onnx"
    if file_matches(model_file, expected_size=G2PW_ONNX_SIZE, expected_sha256=G2PW_ONNX_SHA256):
        return
    archive = DOWNLOAD_DIR / "G2PWModel_1.1.zip"
    ensure_zip_download(G2PW_URL, archive)
    staging_root = TEXT_DIR / (".g2pw-extract-" + str(os.getpid()))
    if staging_root.exists():
        shutil.rmtree(staging_root)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    print("Extracting G2PW model...")
    with zipfile.ZipFile(archive) as package:
        safe_extract_all(package, staging_root)
    candidates = [staging_root / "G2PWModel", staging_root / "G2PWModel_1.1"]
    extracted = next((path for path in candidates if path.is_dir()), None)
    try:
        if extracted is None:
            raise RuntimeError("G2PW 模型压缩包目录结构无效")
        extracted_model = extracted / "g2pW.onnx"
        if not file_matches(extracted_model, expected_size=G2PW_ONNX_SIZE, expected_sha256=G2PW_ONNX_SHA256):
            raise RuntimeError("G2PW g2pW.onnx 大小或 SHA256 校验失败")
        if model_dir.exists():
            shutil.rmtree(model_dir)
        extracted.rename(model_dir)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def ensure_fasttext_lid() -> bool:
    """优先准备完整语言检测模型；失败时允许使用依赖包内置的轻量模型。"""

    target = TARGET_DIR / "fast_langdetect" / "lid.176.bin"
    if file_matches(target, expected_size=FASTTEXT_LID_SIZE, expected_sha256=FASTTEXT_LID_SHA256):
        print(f"fastText language model already exists: {target}")
        return True
    if target.exists():
        target.unlink()

    urls = []
    if os.environ.get("OWVOICE_LID_URL"):
        urls.append(os.environ["OWVOICE_LID_URL"])
    urls.append(FASTTEXT_LID_URL)
    last_error = None
    for url in urls:
        try:
            print(f"Downloading fastText language model from {url}...")
            download_file(
                url,
                target,
                attempts=4,
                expected_size=FASTTEXT_LID_SIZE,
                expected_sha256=FASTTEXT_LID_SHA256,
            )
            print("fastText language model is ready.")
            return True
        except Exception as exc:  # noqa: BLE001 - 继续尝试备用源并允许轻量模型降级
            last_error = exc
            print(f"fastText language model download failed: {exc}")
    print(
        "警告：完整语言检测模型下载失败，将使用 fast_langdetect 自带的轻量模型。"
        f" 如需补下载，可重新运行 setup.bat。原因：{last_error}"
    )
    return False


def download(*, training: bool = False) -> None:
    ensure_ffmpeg()
    ensure_g2pw()
    if not ready(training=training):
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("huggingface_hub is missing; GPT-SoVITS dependencies were not installed.") from exc

        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        repo_id = os.environ.get("OWVOICE_PRETRAINED_REPO", DEFAULT_PRETRAINED_REPO)
        revision = os.environ.get(
            "OWVOICE_PRETRAINED_REVISION",
            DEFAULT_PRETRAINED_REVISION if repo_id == DEFAULT_PRETRAINED_REPO else "main",
        )
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
                    "revision": revision,
                    "allow_patterns": ALLOW_PATTERNS + (TRAINING_ALLOW_PATTERNS if training else []),
                    "local_dir": str(TARGET_DIR),
                }
                if endpoint:
                    kwargs["endpoint"] = endpoint
                snapshot_download(**kwargs)
                if not ready(training=training):
                    required = REQUIRED_FILES + (TRAINING_REQUIRED_FILES if training else ())
                    missing = [str(path.relative_to(TARGET_DIR)) for path in required if not path.is_file()]
                    raise RuntimeError("下载源返回成功但资源仍缺失：" + ", ".join(missing))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                print(f"Download endpoint failed: {endpoint or 'huggingface.co'}: {exc}")
        if last_error is not None:
            raise last_error

    ensure_fasttext_lid()
    if ready(training=training):
        scope = "training and inference" if training else "inference"
        print(f"GPT-SoVITS {scope} pretrained assets already exist.")
        return
    required = REQUIRED_FILES + (TRAINING_REQUIRED_FILES if training else ())
    missing = [str(path.relative_to(TARGET_DIR)) for path in required if not path.is_file()]
    raise RuntimeError("Pretrained asset download incomplete: " + ", ".join(missing))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--training",
        action="store_true",
        help="同时下载本地训练所需的预训练模型；普通首次配置不下载这些大文件",
    )
    args = parser.parse_args()
    download(training=args.training)
