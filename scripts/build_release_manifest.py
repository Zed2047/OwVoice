"""生成发布资产对应的统一 update.json。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.release_manifest import build_manifest


def _release_notes(project_root: Path, version: str) -> str:
    """读取 CHANGELOG 中对应版本的说明；找不到时返回空字符串。"""

    changelog_path = Path(project_root) / "CHANGELOG.md"
    try:
        content = changelog_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = f"## {version}"
    start = content.find(marker)
    if start < 0:
        return ""
    start = content.find("\n", start)
    if start < 0:
        return ""
    body = content[start + 1 :]
    next_heading = body.find("\n## ")
    if next_heading >= 0:
        body = body[:next_heading]
    return body.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    published_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = build_manifest(
        args.version,
        args.archive,
        args.project_root,
        published_at=published_at,
        release_notes=_release_notes(args.project_root, args.version),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
