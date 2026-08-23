#!/usr/bin/env python3
"""Backfill Claude.ai canonical history without touching legacy Card turns.

The command is dry-run by default.  ``--apply`` is the only write path; it never
calls a model and always ingests the tree with Card projection disabled because
the same archives already have a legacy turns projection.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import loaders  # noqa: E402
from claude_ai_adapter import build_claude_ai_tree  # noqa: E402
from config import load_settings  # noqa: E402


DEFAULT_DB = ROOT / "data" / "fragments.db"


def _default_origin_data() -> Path | None:
    configured = load_settings().get("ingest", {}).get("origin_data_dir")
    return Path(configured) if configured else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply Claude.ai canonical history backfill."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--origin-data", type=Path, default=_default_origin_data())
    parser.add_argument(
        "--room",
        required=True,
        help="Explicit canonical room; source authorization is checked on apply.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write canonical nodes/observations. Without this flag the command is read-only.",
    )
    args = parser.parse_args(argv)

    if args.origin_data is None:
        parser.error(
            "需要 --origin-data，或在 settings.ingest.origin_data_dir 配置默认目录"
        )
    paths = sorted(args.origin_data.glob("**/conversations.json"), key=str)
    if not paths:
        print(
            f"no conversations.json files found under {args.origin_data}",
            file=sys.stderr,
        )
        return 1

    exported = build_claude_ai_tree(paths, room=args.room)
    report: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "source": exported.envelope["source"],
        "room": args.room,
        **exported.report,
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    batch = loaders.load_conversation_tree(
        exported.envelope,
        Path(f"claude-ai-{args.room}-tree.json"),
    )
    conn = db.connect(args.db)
    before = _state(conn)
    db.ingest_conversation_tree(conn, batch, project_cards=False)
    after = _state(conn)
    report["result"] = {
        "stored_nodes": after["conversation_nodes"],
        "stored_observations": after["conversation_observations"],
        "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
        "turn_watermark_before": before["turn_watermark"],
        "turn_watermark_after": after["turn_watermark"],
    }
    conn.close()
    protected = ("messages", "turns", "cards", "model_calls", "turn_watermark")
    changed = [name for name in protected if before[name] != after[name]]
    if changed:
        raise RuntimeError(
            "history-only Claude.ai backfill changed protected state: "
            + ", ".join(changed)
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _state(conn) -> dict[str, int]:
    state = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "messages",
            "turns",
            "cards",
            "model_calls",
            "conversation_nodes",
            "conversation_observations",
        )
    }
    state["turn_watermark"] = conn.execute(
        "SELECT revision FROM change_watermarks WHERE name = 'turns'"
    ).fetchone()[0]
    return state


if __name__ == "__main__":
    raise SystemExit(main())
