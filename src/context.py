"""Context module: render the recent-window event-card file per room.

Reads cards from the database and writes each room's `cards-last-24.md`.
Talks to storage only through the passed-in connection; does not import the
card-gen, index, or search modules.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from config import ROOMS, ROOM_DIRS, ROOM_SLUGS, load_settings
from db import card_visible_clause
from timefmt import configured_timezone, format_local_date_time


def cards_path_for(viewer: str) -> Path:
    if viewer not in ROOM_DIRS:
        raise ValueError(f"unknown room for cards output: {viewer!r}")
    return ROOM_DIRS[viewer] / "cards-last-24.md"


# Compatibility aliases for callers written before the output file was renamed.
context_path_for = cards_path_for


def rebuild_context(conn: sqlite3.Connection, viewer: str | None = None) -> None:
    for room in ROOMS if viewer is None else (viewer,):
        rebuild_context_for(conn, room)


def rebuild_context_for(conn: sqlite3.Connection, viewer: str) -> None:
    s = load_settings().get("context", {})
    display_tz = configured_timezone()
    today = dt.datetime.now(display_tz).strftime("%Y-%m-%d")
    lookback = s.get("lookback_hours", 24)
    max_cards = s.get("max_cards", 80)
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=lookback)).isoformat()
    room = ROOM_SLUGS.get(viewer, viewer)
    visible_clause, visible_params = card_visible_clause(viewer, "cards")
    rows = conn.execute(
        f"""
        SELECT card_id, session_id, turn_start, turn_end, headline, share, private, timestamp, room
        FROM cards
        WHERE timestamp >= ?
          AND {visible_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (cutoff, *visible_params, max_cards),
    ).fetchall()
    rows = dedupe_context_rows(conn, rows)
    lines = [
        "# cards — last 24h",
        "",
        f"最近{lookback}小时的事件卡摘要。过期条目从这里淡出，但仍留在 SQLite 中供 recall 检索；"
        f"行尾是卡片 id，`search.sh --card ID` 直接展开全文。",
    ]
    current_date = None
    for row in rows:
        date_str, time_str = format_local_date_time(row["timestamp"], display_tz)
        if date_str != current_date:
            current_date = date_str
            lines.append("")
            today_tag = "（今天）" if date_str == today else ""
            lines.append(f"--{date_str}{today_tag}--")
        content = row["headline"]
        room_tag = f" [{row['room']}]" if row["room"] != room else ""
        lines.append(f"- {time_str} {content}{room_tag} ·{row['card_id']}")
    path = cards_path_for(viewer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dedupe_context_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Hide exact duplicate card spans caused by copied Claude sessions.

    v4 intentionally keeps one turn occurrence per session. Context is a short
    working-memory view, though, so repeated sessions with the same underlying
    source_uuid span should appear once.
    """
    seen: set[tuple[str, ...] | tuple[str, str]] = set()
    kept: list[sqlite3.Row] = []
    for row in rows:
        signature = card_source_span_signature(conn, row)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(row)
    return kept


def card_source_span_signature(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, ...] | tuple[str, str]:
    if row["turn_start"] is None:
        return ("card", row["card_id"])
    rows = conn.execute(
        """
        SELECT source_uuid
        FROM turns
        WHERE session_id = ?
          AND round >= ?
          AND (? IS NULL OR round <= ?)
        ORDER BY round, message_seq, line_no
        """,
        (row["session_id"], row["turn_start"], row["turn_end"], row["turn_end"]),
    ).fetchall()
    if not rows:
        return ("card", row["card_id"])
    return tuple(r["source_uuid"] for r in rows)
