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
    parser.add_argument(
        "--repair-portable-noise",
        action="store_true",
        help=(
            "Repair history-only Claude nodes previously misclassified as messages "
            "before idempotent ingest."
        ),
    )
    parser.add_argument(
        "--repair-history-structure",
        action="store_true",
        help=(
            "Repair history-only Claude parent edges previously copied from "
            "runtime retry/compaction bookkeeping."
        ),
    )
    args = parser.parse_args(argv)
    repair_requested = args.repair_portable_noise or args.repair_history_structure
    if args.project_cards and repair_requested:
        parser.error("history repair is only valid for history-only ingest")

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

    if args.dry_run and not repair_requested:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    conn = db.connect(args.db)
    if repair_requested:
        report["history_repair"] = _repair_history_only_nodes(
            conn,
            batches,
            apply=not args.dry_run,
            allow_portable_noise=args.repair_portable_noise,
            allow_parent_structure=args.repair_history_structure,
        )
    if args.dry_run:
        conn.close()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

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


def _repair_history_only_nodes(
    conn,
    batches,
    *,
    apply: bool,
    allow_portable_noise: bool,
    allow_parent_structure: bool,
) -> dict[str, object]:
    """Apply explicitly selected corrections to unprojected Claude history.

    Canonical nodes are normally immutable. This recovery path accepts only the
    two adapter corrections named by the caller: message -> structural-node
    demotion and/or normalized parent replacement. Source identity, timestamp,
    category, and portable message content otherwise remain immutable. Any Card
    projection anywhere in the enrolled Claude tree makes topology repair unsafe
    and is a hard failure.
    """
    stable_fields = (
        "source", "room", "native_node_id", "occurred_at", "source_type",
    )
    parent_fields = ("parent_node_id", "native_parent_node_id")
    portable_fields = ("kind", "role", "text", "message_json", "provider", "model")
    repairs: list[tuple[object, bool, bool]] = []
    for batch in batches:
        for node in batch.nodes:
            existing = conn.execute(
                "SELECT * FROM conversation_nodes WHERE node_id = ?",
                (node.node_id,),
            ).fetchone()
            if existing is None:
                continue
            expected = {
                field: getattr(node, field)
                for field in stable_fields + parent_fields + portable_fields
            }
            conflicts = [field for field, value in expected.items() if existing[field] != value]
            if not conflicts:
                continue
            needs_parent = any(field in conflicts for field in parent_fields)
            needs_portable = any(field in conflicts for field in portable_fields)
            portable_repairable = (
                not needs_portable
                or (
                    existing["kind"] == "message"
                    and node.kind in {"event", "tool", "checkpoint"}
                    and all(expected[field] is None for field in portable_fields[1:])
                )
            )
            repairable = (
                not any(field in conflicts for field in stable_fields)
                and set(conflicts).issubset(set(parent_fields + portable_fields))
                and portable_repairable
                and (not needs_parent or allow_parent_structure)
                and (not needs_portable or allow_portable_noise)
            )
            if not repairable:
                raise ValueError(
                    f"non-repairable immutable Claude tree conflict for {node.native_node_id!r}: "
                    + ", ".join(conflicts)
                )
            repairs.append((node, needs_parent, needs_portable))

    if repairs:
        sources = sorted({batch.source for batch in batches})
        rooms = sorted({batch.room for batch in batches})
        source_placeholders = ",".join("?" for _ in sources)
        room_placeholders = ",".join("?" for _ in rooms)
        scope = (
            f"n.source IN ({source_placeholders}) "
            f"AND n.room IN ({room_placeholders})"
        )
        params = sources + rooms
        projection_counts = {
            "messages": conn.execute(
                f"""
                SELECT COUNT(*) FROM messages m
                JOIN conversation_nodes n ON n.node_id = m.source_uuid
                WHERE {scope}
                """,
                params,
            ).fetchone()[0],
            "branch_memberships": conn.execute(
                f"""
                SELECT COUNT(*) FROM conversation_node_branches b
                JOIN conversation_nodes n ON n.node_id = b.node_id
                WHERE {scope}
                """,
                params,
            ).fetchone()[0],
            "card_memberships": conn.execute(
                f"""
                SELECT COUNT(*) FROM card_nodes c
                JOIN conversation_nodes n ON n.node_id = c.node_id
                WHERE {scope}
                """,
                params,
            ).fetchone()[0],
            "branch_anchors": conn.execute(
                f"""
                SELECT COUNT(*) FROM conversation_card_branches b
                JOIN conversation_nodes n
                  ON n.node_id = b.anchor_node_id OR n.node_id = b.fork_node_id
                WHERE {scope}
                """,
                params,
            ).fetchone()[0],
        }
        if any(projection_counts.values()):
            raise RuntimeError(
                "Claude history repair refused because the source has Card projections: "
                + json.dumps(projection_counts, sort_keys=True)
            )
    else:
        projection_counts = {
            "messages": 0,
            "branch_memberships": 0,
            "card_memberships": 0,
            "branch_anchors": 0,
        }

    target_kinds: dict[str, int] = {}
    parent_repairs = portable_repairs = 0
    for node, needs_parent, needs_portable in repairs:
        parent_repairs += needs_parent
        portable_repairs += needs_portable
        if needs_portable:
            target_kinds[node.kind] = target_kinds.get(node.kind, 0) + 1
    if apply and repairs:
        conn.execute("SAVEPOINT repair_claude_history")
        try:
            for node, needs_parent, needs_portable in repairs:
                if needs_parent:
                    conn.execute(
                        """
                        UPDATE conversation_nodes
                        SET parent_node_id = ?, native_parent_node_id = ?
                        WHERE node_id = ?
                        """,
                        (node.parent_node_id, node.native_parent_node_id, node.node_id),
                    )
                if needs_portable:
                    conn.execute(
                        """
                        UPDATE conversation_nodes
                        SET kind = ?, role = NULL, text = NULL, message_json = NULL,
                            provider = NULL, model = NULL
                        WHERE node_id = ? AND kind = 'message'
                        """,
                        (node.kind, node.node_id),
                    )
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError("Claude history repair violated foreign keys")
            conn.execute("RELEASE SAVEPOINT repair_claude_history")
            conn.commit()
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT repair_claude_history")
            conn.execute("RELEASE SAVEPOINT repair_claude_history")
            raise
    return {
        "mode": "applied" if apply else "dry-run",
        "nodes": len(repairs),
        "parent_edges": parent_repairs,
        "portable_nodes": portable_repairs,
        "target_kinds": target_kinds,
        "projection_counts": projection_counts,
    }


def _repair_portable_noise(conn, batches, *, apply: bool) -> dict[str, object]:
    """Backward-compatible helper for the original narrow recovery path."""
    return _repair_history_only_nodes(
        conn,
        batches,
        apply=apply,
        allow_portable_noise=True,
        allow_parent_structure=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
