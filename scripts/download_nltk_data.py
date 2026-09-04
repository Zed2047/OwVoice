"""将 GPT-SoVITS 英文处理所需的 NLTK 数据安装到项目目录。"""

from __future__ import annotations

import hashlib
import http.client
import os
import sys
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import nltk


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("OWVOICE_NLTK_DATA", str(PROJECT_DIR / "data" / "nltk_data")))
PACKAGES = {
    # g2p_en 2.1.0 仍会检查旧包；新版 NLTK 的 pos_tag 则需要 _eng 包。
    "averaged_perceptron_tagger": {
        "url": "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/taggers/averaged_perceptron_tagger.zip",
        "sha256": "e1f13cf2532daadfd6f3bc481a49859f0b8ea6432ccdcd83e6a49a5f19008de9",
    },
    "averaged_perceptron_tagger_eng": {
        "url": "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/taggers/averaged_perceptron_tagger_eng.zip",
        "sha256": "6025f530624335c67d6547d44757b357b4e79bae030a0383e9887a92c1718f0b",
    },
    "cmudict": {
        "url": "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/cmudict.zip",
        "sha256": "d07cca47fd72ad32ea9d8ad1219f85301eeaf4568f8b6b73747506a71fb5afd6",
    },
}
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


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    nltk.data.path.insert(0, str(DATA_DIR))
    for package, package_info in PACKAGES.items():
        url = package_info["url"]
        category = "corpora" if package == "cmudict" else "taggers"
        zip_path = DATA_DIR / category / f"{package}.zip"
        resource_dir = DATA_DIR / category / package
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        if resource_dir.is_dir():
            print(f"NLTK package already installed: {package}")
            continue
        print(f"Downloading NLTK package: {package}")
        try:
            download_package(url, zip_path, package_info["sha256"])
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(DATA_DIR / category)
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
