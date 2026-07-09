#!/usr/bin/env python3
"""Find duplicate or highly overlapping cards for manual cleanup.

This is intentionally read-only. It compares cards by the source_uuid span they
cover in turns/messages, which catches copied Claude Code sessions that produced
multiple cards for the same underlying transcript.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fragments.db"
DEFAULT_MD = ROOT / "data" / "duplicate-card-candidates.md"
DEFAULT_JSON = ROOT / "data" / "duplicate-card-candidates.json"

sys.path.insert(0, str(ROOT / "src"))
from timefmt import LOCAL_TZ  # noqa: E402  （settings.timezone）


@dataclass
class Card:
    card_id: str
    session_id: str
    turn_start: int | None
    turn_end: int | None
    timestamp: str
    local_time: str
    room: str
    theme: str
    source_count: int
    source_files: list[str]
    signature: list[str]

    @property
    def is_claude_code(self) -> bool:
        return any("/.claude/projects/" in source_file for source_file in self.source_files)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def local_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def load_cards(conn: sqlite3.Connection, claude_code_only: bool) -> list[Card]:
    rows = conn.execute(
        """
        SELECT card_id, session_id, turn_start, turn_end, timestamp, room, theme
        FROM cards
        ORDER BY timestamp DESC, card_id
        """
    ).fetchall()
    cards: list[Card] = []
    for row in rows:
        if row["turn_start"] is None:
            continue
        turn_rows = conn.execute(
            """
            SELECT t.source_uuid, t.source_file
            FROM turns t
            WHERE t.session_id = ?
              AND t.round >= ?
              AND (? IS NULL OR t.round <= ?)
            ORDER BY t.round, t.message_seq, t.line_no
            """,
            (row["session_id"], row["turn_start"], row["turn_end"], row["turn_end"]),
        ).fetchall()
        signature = [r["source_uuid"] for r in turn_rows]
        if not signature:
            continue
        source_files = sorted({r["source_file"] or "" for r in turn_rows if r["source_file"]})
        card = Card(
            card_id=row["card_id"],
            session_id=row["session_id"],
            turn_start=row["turn_start"],
            turn_end=row["turn_end"],
            timestamp=row["timestamp"] or "",
            local_time=local_time(row["timestamp"]),
            room=row["room"] or "",
            theme=row["theme"] or "",
            source_count=len(signature),
            source_files=source_files,
            signature=signature,
        )
        if claude_code_only and not card.is_claude_code:
            continue
        cards.append(card)
    return cards


def overlap(a: Card, b: Card) -> tuple[int, float, float]:
    sa = set(a.signature)
    sb = set(b.signature)
    inter = len(sa & sb)
    if not inter:
        return 0, 0.0, 0.0
    jaccard = inter / len(sa | sb)
    containment = inter / min(len(sa), len(sb))
    return inter, jaccard, containment


def connected_components(cards: list[Card], threshold: float) -> list[list[Card]]:
    n = len(cards)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            _, jaccard, containment = overlap(cards[i], cards[j])
            if jaccard == 1.0 or containment >= threshold:
                union(i, j)

    groups: dict[int, list[Card]] = {}
    for idx, card in enumerate(cards):
        groups.setdefault(find(idx), []).append(card)
    return [
        sorted(group, key=lambda c: (c.timestamp, c.card_id), reverse=True)
        for group in groups.values()
        if len(group) > 1
    ]


def group_metrics(group: list[Card]) -> dict[str, float | int]:
    max_jaccard = 0.0
    max_containment = 0.0
    min_count = min(c.source_count for c in group)
    max_count = max(c.source_count for c in group)
    for i, card in enumerate(group):
        for other in group[i + 1 :]:
            _, jaccard, containment = overlap(card, other)
            max_jaccard = max(max_jaccard, jaccard)
            max_containment = max(max_containment, containment)
    return {
        "cards": len(group),
        "min_source_count": min_count,
        "max_source_count": max_count,
        "max_jaccard": round(max_jaccard, 3),
        "max_containment": round(max_containment, 3),
    }


def write_markdown(path: Path, groups: list[list[Card]], threshold: float) -> None:
    lines = [
        "# Duplicate Card Candidates",
        "",
        f"Generated by `scripts/find_duplicate_cards.py` with containment threshold `{threshold}`.",
        "Default scope is Claude Code JSONL sourced cards only.",
        "",
        "Manual cleanup idea: keep one card per real event/span, then delete the rejected card ids.",
        "",
    ]
    for idx, group in enumerate(groups, start=1):
        metrics = group_metrics(group)
        lines.append(f"## Group {idx} ({metrics['cards']} cards)")
        lines.append(
            f"- source_uuid count: {metrics['min_source_count']}..{metrics['max_source_count']}; "
            f"max_jaccard={metrics['max_jaccard']}; max_containment={metrics['max_containment']}"
        )
        lines.append("")
        for card in group:
            source_hint = Path(card.source_files[0]).name if card.source_files else ""
            turn_range = f"{card.turn_start}-{card.turn_end}"
            lines.append(f"- [ ] `{card.card_id}` `{card.session_id[:8]}` R{turn_range} {card.local_time} `{card.room}`")
            lines.append(f"  theme: {card.theme}")
            lines.append(f"  source: `{source_hint}`")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_json(path: Path, groups: list[list[Card]], threshold: float) -> None:
    payload = {
        "threshold": threshold,
        "groups": [
            {
                "metrics": group_metrics(group),
                "cards": [
                    {k: v for k, v in asdict(card).items() if k != "signature"}
                    for card in group
                ],
            }
            for group in groups
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--threshold", type=float, default=0.85, help="Containment overlap threshold.")
    parser.add_argument("--all-sources", action="store_true", help="Include non-Claude-Code cards too.")
    args = parser.parse_args()

    conn = connect(args.db)
    cards = load_cards(conn, claude_code_only=not args.all_sources)
    groups = connected_components(cards, args.threshold)
    groups.sort(key=lambda group: (max(c.timestamp for c in group), len(group)), reverse=True)

    write_markdown(args.md, groups, args.threshold)
    write_json(args.json, groups, args.threshold)

    total_cards = sum(len(group) for group in groups)
    print(f"scanned_cards: {len(cards)}")
    print(f"candidate_groups: {len(groups)}")
    print(f"candidate_cards: {total_cards}")
    print(f"markdown: {args.md}")
    print(f"json: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
