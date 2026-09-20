"""校验 resource-lock.json 的 schema 2 供应链字段。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resource_lock import load_resource_lock


def verify(project_root: Path) -> dict:
    project_root = project_root.resolve()
    lock = load_resource_lock(project_root / "resource-lock.json")
    resources = lock["resources"]
    archive_bytes = sum(
        int(resource["archive"]["size_bytes"])
        for resource in resources
        if resource.get("archive") is not None
    )
    installed_files = sum(len(resource["installed_files"]) for resource in resources)
    optional = [resource["id"] for resource in resources if resource.get("status") == "optional_acceleration"]
    return {
        "ok": True,
        "code": "OK",
        "schema": lock["schema"],
        "resources": len(resources),
        "installed_files": installed_files,
        "archive_bytes": archive_bytes,
        "optional_acceleration": optional,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = verify(args.project_root)
    except (OSError, RuntimeError) as exc:
        result = {"ok": False, "code": "RESOURCE_LOCK_INVALID", "failures": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
