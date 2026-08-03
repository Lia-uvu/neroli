#!/usr/bin/env python3
"""Backfill Claude Code canonical history without replaying legacy Card turns."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import loaders  # noqa: E402
from claude_code_adapter import export_claude_code_tree  # noqa: E402
from config import PROJECT_DIRS, ROOMS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Claude Code canonical trees without calling a model."
    )
    parser.add_argument("--db", type=Path, default=db.DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--project-cards",
        action="store_true",
        help="Explicitly project tree messages into Card turns (off for legacy transition).",
    )
    args = parser.parse_args(argv)

    batches = []
    report: dict[str, object] = {"rooms": {}, "project_cards": args.project_cards}
    for room, project_dir in zip(ROOMS, PROJECT_DIRS):
        paths = sorted(project_dir.glob("*.jsonl"), key=str)
        envelope = export_claude_code_tree(paths, room=room)
        batch = loaders.load_conversation_tree(
            envelope, Path(f"claude-code-{room}-tree.json")
        )
        batches.append(batch)
        report["rooms"][room] = {
            "files": len(paths),
            "nodes": len(batch.nodes),
            "observations": len(batch.observations),
        }

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    conn = db.connect(args.db)
    before_revision = _turn_revision(conn)
    before_model_calls = _count(conn, "model_calls")
    changed_sessions: list[str] = []
    for batch in batches:
        changed_sessions.extend(
            db.ingest_conversation_tree(
                conn, batch, project_cards=args.project_cards
            )
        )
    after_revision = _turn_revision(conn)
    after_model_calls = _count(conn, "model_calls")
    report["result"] = {
        "stored_nodes": _count(conn, "conversation_nodes"),
        "stored_observations": _count(conn, "conversation_observations"),
        "changed_card_sessions": len(set(changed_sessions)),
        "turn_watermark_before": before_revision,
        "turn_watermark_after": after_revision,
        "model_calls_before": before_model_calls,
        "model_calls_after": after_model_calls,
        "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
    }
    conn.close()
    if not args.project_cards and (
        before_revision != after_revision or before_model_calls != after_model_calls
    ):
        raise RuntimeError("history-only Claude tree backfill changed Card/model state")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _turn_revision(conn) -> int:
    return conn.execute(
        "SELECT revision FROM change_watermarks WHERE name='turns'"
    ).fetchone()[0]


if __name__ == "__main__":
    raise SystemExit(main())
