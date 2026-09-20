"""将 GPT-SoVITS 英文处理所需的 NLTK 数据安装到项目目录。"""

from __future__ import annotations

import json
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path

import nltk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resource_lock import installed_file_map, index_resources, load_resource_lock, primary_source
from resource_download import ensure_zip_download, file_matches, safe_extract_all


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("OWVOICE_NLTK_DATA", str(PROJECT_DIR / "data" / "nltk_data")))
RESOURCE_LOCK_PATH = PROJECT_DIR / "resource-lock.json"
try:
    RESOURCE_LOCK = load_resource_lock(RESOURCE_LOCK_PATH)
    RESOURCE_BY_ID = index_resources(RESOURCE_LOCK)
    PACKAGE_RESOURCES = {
        resource["package_name"]: resource
        for resource in RESOURCE_LOCK["resources"]
        if str(resource["id"]).startswith("nltk-")
    }
    PACKAGES = {
        package: {
            "url": primary_source(resource),
            "size_bytes": resource["archive"]["size_bytes"],
            "sha256": resource["archive"]["sha256"],
            "installed_files": installed_file_map(resource),
        }
        for package, resource in PACKAGE_RESOURCES.items()
    }
except (OSError, ValueError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
    raise RuntimeError(f"资源锁文件无效：{RESOURCE_LOCK_PATH}") from exc
RESOURCE_PATHS = (
    DATA_DIR / "taggers" / "averaged_perceptron_tagger",
    DATA_DIR / "taggers" / "averaged_perceptron_tagger_eng",
    DATA_DIR / "corpora" / "cmudict",
)


def download_package(url: str, target: Path, expected_size: int, expected_sha256: str) -> None:
    """通过共享下载器获取并校验 NLTK ZIP。"""

    ensure_zip_download(
        url,
        target,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )


def installed_ready(package_info: dict) -> bool:
    return all(
        file_matches(DATA_DIR / relative, size, sha256)
        for relative, (size, sha256) in package_info["installed_files"].items()
    )


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    nltk.data.path.insert(0, str(DATA_DIR))
    for package, package_info in PACKAGES.items():
        url = package_info["url"]
        category = "corpora" if package == "cmudict" else "taggers"
        zip_path = DATA_DIR / category / f"{package}.zip"
        resource_dir = DATA_DIR / category / package
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        if installed_ready(package_info):
            print(f"NLTK package already installed: {package}")
            continue
        print(f"Downloading NLTK package: {package}")
        try:
            download_package(url, zip_path, package_info["size_bytes"], package_info["sha256"])
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
                if not installed_ready(package_info):
                    raise RuntimeError(f"NLTK 安装文件校验失败：{package}")
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
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
