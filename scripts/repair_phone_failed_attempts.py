#!/usr/bin/env python3
"""Remove phone requests explicitly marked as undelivered from Neroli.

Dry-run is the default. This never guesses from error text or message content:
only ``phone-error.parentUuid -> user.uuid`` pairs in phone journals qualify.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import PROJECT_DIRS  # noqa: E402
from db import DB, connect, refresh_session_forks  # noqa: E402


@dataclass(frozen=True)
class FailedAttempt:
    source_file: Path
    user_uuid: str


def failed_attempts() -> list[FailedAttempt]:
    found: set[tuple[str, str]] = set()
    for project_dir in PROJECT_DIRS:
        for path in project_dir.glob("phone-*.jsonl"):
            if path.name.endswith(".annotations.jsonl"):
                continue
            rows: list[dict] = []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict):
                        rows.append(raw)
            by_uuid = {raw.get("uuid"): raw for raw in rows if raw.get("uuid")}
            for raw in rows:
                parent = raw.get("parentUuid")
                if (
                    raw.get("type") == "phone-error"
                    and isinstance(parent, str)
                    and by_uuid.get(parent, {}).get("type") == "user"
                ):
                    found.add((str(path), parent))
    return [FailedAttempt(Path(path), uuid) for path, uuid in sorted(found)]


def inspect(conn: sqlite3.Connection, attempts: list[FailedAttempt]) -> dict:
    legacy_turns: list[sqlite3.Row] = []
    tree_nodes: list[sqlite3.Row] = []
    cards: dict[str, sqlite3.Row] = {}
    for attempt in attempts:
        turns = conn.execute(
            """
            SELECT id, session_id, source_uuid, round
            FROM turns WHERE source_uuid = ? AND source_file = ?
            """,
            (attempt.user_uuid, str(attempt.source_file)),
        ).fetchall()
        legacy_turns.extend(turns)
        for turn in turns:
            for card in conn.execute(
                """
                SELECT card_id, session_id, turn_start, turn_end
                FROM cards
                WHERE session_id = ? AND turn_start <= ? AND turn_end >= ?
                """,
                (turn["session_id"], turn["round"], turn["round"]),
            ).fetchall():
                cards[card["card_id"]] = card
        tree_nodes.extend(conn.execute(
            """
            SELECT node_id, native_node_id, kind, source_type
            FROM conversation_nodes
            WHERE native_node_id = ? AND source = 'claude-code' AND kind = 'message'
            """,
            (attempt.user_uuid,),
        ).fetchall())
    return {
        "legacy_turns": legacy_turns,
        "tree_nodes": tree_nodes,
        "cards": list(cards.values()),
    }


def apply_repair(conn: sqlite3.Connection, report: dict) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for card in report["cards"]:
            conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card["card_id"],))
            conn.execute("DELETE FROM cards WHERE card_id = ?", (card["card_id"],))

        legacy_ids = {row["source_uuid"] for row in report["legacy_turns"]}
        for row in report["legacy_turns"]:
            conn.execute("DELETE FROM turns WHERE id = ?", (row["id"],))
        for source_uuid in legacy_ids:
            conn.execute(
                "DELETE FROM messages WHERE source_uuid = ? "
                "AND NOT EXISTS (SELECT 1 FROM turns WHERE source_uuid = ?)",
                (source_uuid, source_uuid),
            )

        for node in report["tree_nodes"]:
            memberships = conn.execute(
                "SELECT COUNT(*) FROM card_nodes WHERE node_id = ?", (node["node_id"],)
            ).fetchone()[0]
            if memberships:
                raise RuntimeError(
                    f"canonical node {node['native_node_id']} still belongs to a Card"
                )
            conn.execute("DELETE FROM turns WHERE source_uuid = ?", (node["node_id"],))
            conn.execute(
                "DELETE FROM messages WHERE source_uuid = ? "
                "AND NOT EXISTS (SELECT 1 FROM turns WHERE source_uuid = ?)",
                (node["node_id"], node["node_id"]),
            )
            conn.execute(
                """
                UPDATE conversation_nodes
                SET kind = 'event', source_type = 'user:phone-failed-attempt',
                    role = NULL, text = NULL, message_json = NULL,
                    provider = NULL, model = NULL
                WHERE node_id = ?
                """,
                (node["node_id"],),
            )
        refresh_session_forks(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and (ROOT / "data/ingest.lock.d").exists():
        raise SystemExit("ingest lock exists; stop watchers and verify the owner before applying")
    conn = connect(args.db) if args.apply else sqlite3.connect(
        f"file:{args.db}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    attempts = failed_attempts()
    report = inspect(conn, attempts)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "failed_attempts": len(attempts),
        "legacy_turns": len(report["legacy_turns"]),
        "canonical_nodes": len(report["tree_nodes"]),
        "cards_to_remove": [row["card_id"] for row in report["cards"]],
    }, ensure_ascii=False, indent=2))
    if args.apply:
        apply_repair(conn, report)
        print("repair applied; run a full ingest to renumber surviving turns")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
