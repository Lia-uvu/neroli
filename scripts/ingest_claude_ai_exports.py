#!/usr/bin/env python3
"""Safely ingest Claude.ai archive exports.

Only conversations.json files are selected. The other JSON files in a Claude.ai
download (projects, users, memories) are metadata, not chat turns.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DB = ROOT / "data" / "fragments.db"

sys.path.insert(0, str(SRC))
from cli import run  # noqa: E402
from config import load_settings  # noqa: E402


def _default_origin_data() -> Path | None:
    configured = load_settings().get("ingest", {}).get("origin_data_dir")
    return Path(configured) if configured else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Claude.ai conversations.json exports into turns v4.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--origin-data", type=Path, default=_default_origin_data(),
                        help="导出归档目录（默认 settings.ingest.origin_data_dir）")
    parser.add_argument("--dry-run", action="store_true", help="List selected conversations.json files without ingesting.")
    args = parser.parse_args()

    if args.origin_data is None:
        print("需要 --origin-data，或在 settings.ingest.origin_data_dir 配置默认目录", file=sys.stderr)
        return 2

    paths = sorted(args.origin_data.glob("**/conversations.json"))
    if not paths:
        print(f"no conversations.json files found under {args.origin_data}", file=sys.stderr)
        return 1

    if args.dry_run:
        for path in paths:
            print(path)
        print(f"{len(paths)} conversations.json files")
        return 0

    argv = ["--db", str(args.db), "--ingest-only", "--max-messages", "0", *[str(path) for path in paths]]
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
