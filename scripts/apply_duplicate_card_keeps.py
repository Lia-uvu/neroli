#!/usr/bin/env python3
"""Apply KEEP decisions from duplicate-card-candidates.md.

Read-only by default. For every candidate group that has at least one KEEP row,
delete the unmarked cards in that group from the derived card layer. Original
messages/turns are never touched.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fragments.db"
DEFAULT_MD = ROOT / "data" / "duplicate-card-candidates.md"

GROUP_RE = re.compile(r"^##\s+Group\s+\d+", re.I)
CARD_RE = re.compile(r"^-\s+\[(?P<mark>[^\]]*)\]\s+`(?P<card_id>[^`]+)`")


@dataclass
class Group:
    cards: list[str]
    keeps: list[str]


def parse_groups(path: Path) -> list[Group]:
    groups: list[Group] = []
    current: Group | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if GROUP_RE.match(line):
            if current is not None:
                groups.append(current)
            current = Group(cards=[], keeps=[])
            continue
        match = CARD_RE.match(line)
        if match and current is not None:
            card_id = match.group("card_id")
            mark = match.group("mark").strip().upper()
            current.cards.append(card_id)
            if mark == "KEEP":
                current.keeps.append(card_id)
    if current is not None:
        groups.append(current)
    return groups


def decisions(groups: list[Group]) -> tuple[list[str], list[str], int]:
    keep: list[str] = []
    delete: list[str] = []
    skipped = 0
    for group in groups:
        if not group.keeps:
            skipped += 1
            continue
        keep.extend(group.keeps)
        delete.extend(card_id for card_id in group.cards if card_id not in set(group.keeps))
    return sorted(set(keep)), sorted(set(delete)), skipped


def fetch_card_rows(conn: sqlite3.Connection, card_ids: list[str]) -> list[sqlite3.Row]:
    if not card_ids:
        return []
    placeholders = ",".join("?" for _ in card_ids)
    return conn.execute(
        f"""
        SELECT card_id, session_id, turn_start, turn_end, timestamp, headline
        FROM cards
        WHERE card_id IN ({placeholders})
        ORDER BY timestamp DESC, card_id
        """,
        card_ids,
    ).fetchall()


def delete_cards(conn: sqlite3.Connection, card_ids: list[str]) -> None:
    for card_id in card_ids:
        conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM cluster_members WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM card_tags WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM card_raw WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    groups = parse_groups(args.md)
    keep, delete, skipped = decisions(groups)
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    missing = sorted(set(delete) - {row["card_id"] for row in fetch_card_rows(conn, delete)})
    mode = "apply" if args.apply else "dry-run"
    print(f"mode: {mode}")
    print(f"groups: {len(groups)}")
    print(f"groups_without_keep: {skipped}")
    print(f"keep_cards: {len(keep)}")
    print(f"delete_cards: {len(delete)}")
    if missing:
        print(f"missing_delete_cards: {len(missing)}")
        for card_id in missing:
            print(f"  missing {card_id}")
        raise SystemExit(1)

    print("\nkeep:")
    for card_id in keep:
        print(f"  {card_id}")

    print("\ndelete:")
    for row in fetch_card_rows(conn, delete):
        turn_range = f"{row['turn_start']}-{row['turn_end']}"
        print(f"  {row['card_id']} {row['session_id'][:8]} R{turn_range} {row['timestamp']} {row['headline']}")

    if args.apply:
        delete_cards(conn, delete)
        print(f"\napplied_delete_cards: {len(delete)}")
    else:
        print("\nno changes written; rerun with --apply to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
