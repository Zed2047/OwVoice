"""将 GPT-SoVITS 英文处理所需的 NLTK 数据安装到项目目录。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import sys
import shutil
import time
import urllib.error
import urllib.request
import zipfile
import tempfile
from pathlib import Path

import nltk


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("OWVOICE_NLTK_DATA", str(PROJECT_DIR / "data" / "nltk_data")))
RESOURCE_LOCK_PATH = PROJECT_DIR / "resource-lock.json"
try:
    RESOURCE_LOCK = json.loads(RESOURCE_LOCK_PATH.read_text(encoding="utf-8"))
    if RESOURCE_LOCK.get("schema") != 1:
        raise ValueError("unsupported schema")
    # g2p_en 2.1.0 仍会检查旧包；新版 NLTK 的 pos_tag 则需要 _eng 包。
    PACKAGES = RESOURCE_LOCK["nltk"]
except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
    raise RuntimeError(f"资源锁文件无效：{RESOURCE_LOCK_PATH}") from exc
RESOURCE_PATHS = (
    DATA_DIR / "taggers" / "averaged_perceptron_tagger",
    DATA_DIR / "taggers" / "averaged_perceptron_tagger_eng",
    DATA_DIR / "corpora" / "cmudict",
)


def download_package(url: str, target: Path, expected_sha256: str) -> None:
    """下载到临时文件并校验，避免中断后留下可被误用的半截 ZIP。"""

    partial = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise OSError(f"SHA256 mismatch: expected {expected_sha256}, got {digest}")
            with zipfile.ZipFile(partial) as archive:
                if archive.testzip() is not None:
                    raise zipfile.BadZipFile("ZIP 内部文件校验失败")
            partial.replace(target)
            return
        except (OSError, urllib.error.URLError, http.client.IncompleteRead, zipfile.BadZipFile) as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"下载 NLTK 数据失败：{url}\n{last_error}") from last_error


def safe_extract_all(archive: zipfile.ZipFile, destination: Path) -> None:
    """拒绝绝对路径、目录穿越和符号链接，避免下载包写出目标目录。"""

    root = destination.resolve()
    for info in archive.infolist():
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise RuntimeError(f"NLTK 压缩包包含不安全链接：{info.filename}")
        output = (destination / info.filename).resolve()
        try:
            output.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"NLTK 压缩包包含不安全路径：{info.filename}") from exc
    archive.extractall(destination)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    nltk.data.path.insert(0, str(DATA_DIR))
    for package, package_info in PACKAGES.items():
        url = package_info["url"]
        category = "corpora" if package == "cmudict" else "taggers"
        zip_path = DATA_DIR / category / f"{package}.zip"
        resource_dir = DATA_DIR / category / package
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        marker = resource_dir / ("README" if package == "cmudict" else "averaged_perceptron_tagger.pickle")
        if package == "averaged_perceptron_tagger_eng":
            marker = resource_dir / "averaged_perceptron_tagger_eng.weights.json"
        if marker.is_file() and marker.stat().st_size > 0:
            print(f"NLTK package already installed: {package}")
            continue
        print(f"Downloading NLTK package: {package}")
        try:
            download_package(url, zip_path, package_info["sha256"])
            with tempfile.TemporaryDirectory(prefix="owvoice-nltk-", dir=zip_path.parent) as temporary:
                temporary_path = Path(temporary)
                with zipfile.ZipFile(zip_path) as archive:
                    safe_extract_all(archive, temporary_path)
                extracted = temporary_path / package
                if not extracted.is_dir():
                    raise RuntimeError(f"NLTK 压缩包目录结构无效：{package}")
                if resource_dir.exists():
                    shutil.rmtree(resource_dir)
                extracted.rename(resource_dir)
        except (OSError, urllib.error.URLError, RuntimeError, zipfile.BadZipFile) as exc:
            print(f"Failed to download NLTK package {package}: {exc}", file=sys.stderr)
            return 1
    missing = [str(path) for path in RESOURCE_PATHS if not path.is_dir()]
    if missing:
        print("NLTK resources are still missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"NLTK data ready: {DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
