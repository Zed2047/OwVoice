"""Download GPT-SoVITS inference assets and, optionally, local-training assets."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
import argparse
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resource_lock import installed_file_map, index_resources, load_resource_lock as read_resource_lock, primary_source
from resource_download import download_file, download_from_sources, ensure_zip_download, file_matches, safe_extract_all
from resource_state import update_resource_state


PROJECT_DIR = Path(__file__).resolve().parents[1]
TARGET_DIR = PROJECT_DIR / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models"
ENGINE_DIR = PROJECT_DIR / "GPT-SoVITS"
TEXT_DIR = ENGINE_DIR / "GPT_SoVITS" / "text"
DOWNLOAD_DIR = PROJECT_DIR / ".cache" / "setup-downloads"
RESOURCE_LOCK_PATH = PROJECT_DIR / "resource-lock.json"
RESOURCE_STATE_PATH = PROJECT_DIR / ".runtime" / "resource-state.json"


def load_resource_lock() -> dict:
    return read_resource_lock(RESOURCE_LOCK_PATH)


RESOURCE_LOCK = load_resource_lock()
RESOURCE_BY_ID = index_resources(RESOURCE_LOCK)
FFMPEG_INFO = RESOURCE_BY_ID["ffmpeg-9.0.1-essentials"]
G2PW_INFO = RESOURCE_BY_ID["g2pw-1.1"]
FASTTEXT_INFO = RESOURCE_BY_ID["fasttext-lid-176"]
PRETRAINED_INFO = RESOURCE_BY_ID["gpt-sovits-pretrained"]
FFMPEG_URL = primary_source(FFMPEG_INFO)
FFMPEG_VERSION = FFMPEG_INFO["version"]
FFMPEG_ARCHIVE_SIZE = FFMPEG_INFO["archive"]["size_bytes"]
FFMPEG_ARCHIVE_SHA256 = FFMPEG_INFO["archive"]["sha256"]
FFMPEG_FILE_CHECKS = installed_file_map(FFMPEG_INFO)
G2PW_URL = primary_source(G2PW_INFO)
G2PW_ARCHIVE_SIZE = G2PW_INFO["archive"]["size_bytes"]
G2PW_ARCHIVE_SHA256 = G2PW_INFO["archive"]["sha256"]
G2PW_FILE_CHECKS = installed_file_map(G2PW_INFO)
G2PW_ONNX_SIZE, G2PW_ONNX_SHA256 = next(iter(G2PW_FILE_CHECKS.values()))
DEFAULT_PRETRAINED_REPO = PRETRAINED_INFO["repo_id"]
DEFAULT_PRETRAINED_REVISION = PRETRAINED_INFO["revision"]
PRETRAINED_FILES = installed_file_map(PRETRAINED_INFO)
FASTTEXT_LID_URL = primary_source(FASTTEXT_INFO)
FASTTEXT_LID_SIZE = FASTTEXT_INFO["archive"]["size_bytes"]
FASTTEXT_LID_SHA256 = FASTTEXT_INFO["archive"]["sha256"]
TRAINING_RELATIVE_FILES = {
    "sv/pretrained_eres2netv2w24s4ep4.ckpt",
    "v2Pro/s2Gv2Pro.pth",
    "v2Pro/s2Dv2Pro.pth",
}
REQUIRED_FILES = tuple(
    TARGET_DIR / relative
    for relative in PRETRAINED_FILES
    if relative not in TRAINING_RELATIVE_FILES
)
TRAINING_REQUIRED_FILES = tuple(TARGET_DIR / relative for relative in sorted(TRAINING_RELATIVE_FILES))
REQUIRED_FILE_CHECKS = {
    TARGET_DIR / relative: check
    for relative, check in PRETRAINED_FILES.items()
}
ALLOW_PATTERNS = [relative for relative in PRETRAINED_FILES if relative not in TRAINING_RELATIVE_FILES]
TRAINING_ALLOW_PATTERNS = sorted(TRAINING_RELATIVE_FILES)


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


def ffmpeg_works(path: Path, *, expected_version: str = FFMPEG_VERSION) -> bool:
    if not path.is_file() or path.stat().st_size < 50 * 1024 * 1024:
        return False
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.startswith(f"ffmpeg version {expected_version}")


def ffprobe_works(path: Path, *, expected_version: str = FFMPEG_VERSION) -> bool:
    if not path.is_file() or path.stat().st_size < 50 * 1024 * 1024:
        return False
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.startswith(f"ffprobe version {expected_version}")


def ensure_ffmpeg() -> None:
    ffmpeg = ENGINE_DIR / "ffmpeg.exe"
    ffprobe = ENGINE_DIR / "ffprobe.exe"
    if (file_matches(ffmpeg, expected_size=FFMPEG_FILE_CHECKS["ffmpeg.exe"][0], expected_sha256=FFMPEG_FILE_CHECKS["ffmpeg.exe"][1]) and
            file_matches(ffprobe, expected_size=FFMPEG_FILE_CHECKS["ffprobe.exe"][0], expected_sha256=FFMPEG_FILE_CHECKS["ffprobe.exe"][1]) and
            ffmpeg_works(ffmpeg) and ffprobe_works(ffprobe)):
        return
    archive = DOWNLOAD_DIR / "ffmpeg-essentials.zip"
    ensure_zip_download(FFMPEG_URL, archive, expected_size=FFMPEG_ARCHIVE_SIZE, expected_sha256=FFMPEG_ARCHIVE_SHA256)
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
    if (not file_matches(ffmpeg, expected_size=FFMPEG_FILE_CHECKS["ffmpeg.exe"][0], expected_sha256=FFMPEG_FILE_CHECKS["ffmpeg.exe"][1]) or
            not file_matches(ffprobe, expected_size=FFMPEG_FILE_CHECKS["ffprobe.exe"][0], expected_sha256=FFMPEG_FILE_CHECKS["ffprobe.exe"][1]) or
            not ffmpeg_works(ffmpeg) or not ffprobe_works(ffprobe)):
        raise RuntimeError("ffmpeg 解压完成但无法运行；压缩包可能损坏或不兼容当前 Windows。")


def ensure_g2pw() -> None:
    model_dir = TEXT_DIR / "G2PWModel"
    model_file = model_dir / "g2pW.onnx"
    if file_matches(model_file, expected_size=G2PW_ONNX_SIZE, expected_sha256=G2PW_ONNX_SHA256):
        return
    archive = DOWNLOAD_DIR / "G2PWModel_1.1.zip"
    ensure_zip_download(
        G2PW_URL,
        archive,
        expected_size=G2PW_ARCHIVE_SIZE,
        expected_sha256=G2PW_ARCHIVE_SHA256,
    )
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
        update_resource_state(RESOURCE_STATE_PATH, FASTTEXT_INFO["id"], mode="full", revision=str(FASTTEXT_INFO["version"]))
        print(f"fastText language model already exists: {target}")
        return True
    if target.exists():
        target.unlink()

    urls = [FASTTEXT_LID_URL]
    if os.environ.get("OWVOICE_ALLOW_UNPINNED_RESOURCES") == "1" and os.environ.get("OWVOICE_LID_URL"):
        urls.insert(0, os.environ["OWVOICE_LID_URL"])
    elif os.environ.get("OWVOICE_LID_URL"):
        print("忽略 OWVOICE_LID_URL：正式安装默认只允许资源锁中的固定来源。")
    last_error = None
    try:
        print(f"Downloading fastText language model from {urls[0]}...")
        download_from_sources(
            urls,
            target,
            attempts=4,
            expected_size=FASTTEXT_LID_SIZE,
            expected_sha256=FASTTEXT_LID_SHA256,
            download_func=download_file,
        )
        update_resource_state(RESOURCE_STATE_PATH, FASTTEXT_INFO["id"], mode="full", revision=str(FASTTEXT_INFO["version"]))
        print("fastText language model is ready.")
        return True
    except Exception as exc:  # noqa: BLE001 - 继续尝试备用源并允许轻量模型降级
        last_error = exc
        print(f"fastText language model download failed: {exc}")
    update_resource_state(
        RESOURCE_STATE_PATH,
        FASTTEXT_INFO["id"],
        mode="lite",
        reason="完整 fastText 模型下载或校验失败；使用 fast_langdetect 内置轻量模型。",
        revision=str(FASTTEXT_INFO["version"]),
    )
    print(
        "警告：完整语言检测模型下载失败，将使用 fast_langdetect 自带的轻量模型。"
        f" 如需补下载，可重新运行 setup.bat。原因：{last_error}"
    )
    return False


def verify_pretrained_files(*, training: bool = False) -> None:
    """按资源锁完整核对已下载模型，防止只检查少数入口文件造成混版。"""

    required = set(REQUIRED_FILES)
    if training:
        required.update(TRAINING_REQUIRED_FILES)
    failures = []
    for path in sorted(required, key=str):
        check = REQUIRED_FILE_CHECKS.get(path)
        if check is None or not file_matches(path, expected_size=check[0], expected_sha256=check[1]):
            failures.append(str(path.relative_to(TARGET_DIR)))
    if failures:
        raise RuntimeError("预训练资源大小或 SHA256 校验失败：" + ", ".join(failures))


def download(*, training: bool = False) -> None:
    ensure_ffmpeg()
    ensure_g2pw()
    if not ready(training=training):
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("huggingface_hub is missing; GPT-SoVITS dependencies were not installed.") from exc

        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        allow_unpinned = os.environ.get("OWVOICE_ALLOW_UNPINNED_RESOURCES") == "1"
        requested_repo = os.environ.get("OWVOICE_PRETRAINED_REPO")
        requested_revision = os.environ.get("OWVOICE_PRETRAINED_REVISION")
        if (requested_repo or requested_revision) and not allow_unpinned:
            print("忽略未固定的预训练资源覆盖；正式安装使用 resource-lock.json。")
        repo_id = requested_repo if allow_unpinned and requested_repo else DEFAULT_PRETRAINED_REPO
        revision = requested_revision if allow_unpinned and requested_revision else DEFAULT_PRETRAINED_REVISION
        print(f"正在准备 GPT-SoVITS 资源：{repo_id}。下载期间会复用已完成文件，请勿关闭窗口。")
        endpoints = [str(source["url"]) for source in PRETRAINED_INFO["sources"]]
        if allow_unpinned and os.environ.get("HF_ENDPOINT"):
            endpoints.insert(0, os.environ["HF_ENDPOINT"])
        last_error = None
        for endpoint in endpoints:
            try:
                print(f"正在连接资源源：{endpoint or 'https://huggingface.co'}")
                kwargs = {
                    "repo_id": repo_id,
                    "revision": revision,
                    "allow_patterns": ALLOW_PATTERNS + (TRAINING_ALLOW_PATTERNS if training else []),
                    "local_dir": str(TARGET_DIR),
                }
                kwargs["endpoint"] = endpoint
                snapshot_download(**kwargs)
                if not ready(training=training):
                    required = REQUIRED_FILES + (TRAINING_REQUIRED_FILES if training else ())
                    missing = [str(path.relative_to(TARGET_DIR)) for path in required if not path.is_file()]
                    raise RuntimeError("下载源返回成功但资源仍缺失：" + ", ".join(missing))
                verify_pretrained_files(training=training)
                print("GPT-SoVITS 资源下载和校验完成。")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                print(f"Download endpoint failed: {endpoint or 'huggingface.co'}: {exc}")
        if last_error is not None:
            raise last_error

    ensure_fasttext_lid()
    if ready(training=training):
        verify_pretrained_files(training=training)
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
