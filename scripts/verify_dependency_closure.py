"""校验发行包依赖/import 契约，防止锁文件遗漏运行时必需包。"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path


PACKAGE_PATTERN = re.compile(r'"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^"\s]+)"')
LOCK_PACKAGE_PATTERN = re.compile(
    r'\[\[package\]\]\s+name\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"',
    re.MULTILINE,
)
FEATURES = ("cpu", "gpu", "training")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"依赖契约无效：{path}") from exc
    if value.get("schema") != 1 or not isinstance(value.get("packages"), list):
        raise RuntimeError("依赖契约 schema 无效")
    return value


def _parse_project_packages(text: str) -> dict[str, set[str]]:
    packages: dict[str, set[str]] = {}
    for distribution, version in PACKAGE_PATTERN.findall(text):
        packages.setdefault(distribution.lower(), set()).add(version)
    return packages


def _parse_lock_packages(text: str) -> dict[str, set[str]]:
    packages: dict[str, set[str]] = {}
    for distribution, version in LOCK_PACKAGE_PATTERN.findall(text):
        packages.setdefault(distribution.lower(), set()).add(version)
    return packages


def _contains_version(versions: set[str], expected: str) -> bool:
    return expected in versions or any(value.startswith(expected + "+") for value in versions)


def verify(project_root: Path, *, check_imports: bool = False) -> dict:
    project_root = project_root.resolve()
    contract = _load_json(project_root / "dependency-contract.json")
    project_text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    lock_text = (project_root / "uv.lock").read_text(encoding="utf-8")
    project = _parse_project_packages(project_text)
    lock = _parse_lock_packages(lock_text)
    failures: list[str] = []

    for entry in contract["packages"]:
        distribution = str(entry.get("distribution", "")).lower()
        version = str(entry.get("version", ""))
        if not distribution or not version or not entry.get("imports"):
            failures.append(f"契约条目不完整：{entry!r}")
            continue
        features = {str(value).lower() for value in entry.get("features", [])}
        if not features or not features.issubset(FEATURES):
            failures.append(f"契约功能范围无效：{distribution}")
        if not _contains_version(project.get(distribution, set()), version):
            failures.append(f"pyproject.toml 缺少 {distribution}=={version}")
        if not _contains_version(lock.get(distribution, set()), version):
            failures.append(f"uv.lock 缺少 {distribution}=={version}")
        if check_imports:
            for module_name in entry["imports"]:
                try:
                    importlib.import_module(str(module_name))
                except Exception as exc:  # noqa: BLE001 - 输出具体缺失模块
                    failures.append(f"import {module_name} 失败：{type(exc).__name__}: {exc}")

    smoke = next(
        (entry for entry in contract["packages"] if entry.get("smoke") == "jieba"),
        None,
    )
    if smoke and check_imports:
        try:
            import jieba  # type: ignore
            import jieba.posseg as posseg  # type: ignore

            words = list(jieba.cut("OwVoice 中文分词"))
            tagged = list(posseg.cut("OwVoice 中文分词"))
            if not words or not tagged:
                failures.append("jieba 功能冒烟结果为空")
        except Exception as exc:  # noqa: BLE001 - 统一转为门禁失败
            failures.append(f"jieba 功能冒烟失败：{type(exc).__name__}: {exc}")

    return {
        "ok": not failures,
        "code": "OK" if not failures else "DEPENDENCY_CLOSURE_FAILED",
        "checked": len(contract["packages"]),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-imports", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.project_root, check_imports=args.check_imports)
    except (OSError, RuntimeError) as exc:
        result = {"ok": False, "code": "DEPENDENCY_CLOSURE_FAILED", "failures": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
