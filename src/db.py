from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from config import DEFAULT_ROOM, MEMORY, ROOM_SLUGS, load_settings, room_for_source_file
from memory_types import Message

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
if VENDOR.exists() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
import jieba  # type: ignore

SCHEMA_VERSION = 7

DB = MEMORY / "data" / "fragments.db"
SCHEMA = MEMORY / "config" / "schema.sql"


def connect(db_path: Path = DB) -> sqlite3.Connection:
    """打开数据库。全新文件按 schema.sql 建表；已有库只校验版本，不做运行时迁移。

    版本不符时报错并指向 migrations/——升级是显式的一次性操作（编号 SQL），
    不是 connect() 里的隐式考古。历史教训见 skills/recall-pipeline/decisions.md。
    """
    is_new = not db_path.exists() or db_path.stat().st_size == 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    if is_new:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"database schema version {version} != expected {SCHEMA_VERSION}; "
            f"apply pending SQL in {MEMORY / 'migrations'} (e.g. sqlite3 {db_path} < migrations/NNN-*.sql)"
        )
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_pipeline_run(conn: sqlite3.Connection, model: str, prompt_file: Path | None, inputs: list[Path]) -> str:
    run_id = dt.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    conn.execute(
        """
        INSERT INTO pipeline_runs (id, model, prompt_file, source_files)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, model, str(prompt_file) if prompt_file else "", json.dumps([str(path) for path in inputs], ensure_ascii=False)),
    )
    conn.commit()
    return run_id


def record_model_call(
    conn: sqlite3.Connection,
    run_id: str,
    step: str,
    prompt: str,
    raw_output: str,
    parsed: Any,
    session_id: str | None = None,
    card_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO model_calls
        (run_id, session_id, card_id, step, prompt, raw_output, parsed_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, session_id, card_id, step, prompt, raw_output, json.dumps(parsed, ensure_ascii=False)),
    )


def ingest_turns(conn: sqlite3.Connection, messages: list[Message]) -> list[str]:
    """v4 两层写入：内容进 messages（首次为准、不可变），出现进 turns（可重排自纠）。

    全量重读会用同样的 source_uuid 重算 round/message_seq；turns DO UPDATE 让编号自我纠正，
    messages DO NOTHING 保内容不变。返回有新/改 turns 行的 session_id。
    """
    session_ids: set[str] = set()
    for message in messages:
        if not message.source_uuid:
            continue  # 没有去重键的消息不入库（理论上不会发生）
        source = getattr(message, "source", "opus-legacy") or "opus-legacy"
        conn.execute(
            """
            INSERT INTO messages
            (source_uuid, role, speaker, text, timestamp, parent_uuid, source, model, has_image, image_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_uuid) DO NOTHING
            """,
            (
                message.source_uuid,
                message.role,
                message.speaker,
                message.text,
                message.timestamp,
                message.parent_uuid,
                source,
                message.model or None,
                message.has_image,
                message.image_count,
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO turns
            (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, source_uuid) DO UPDATE SET
              round = excluded.round,
              message_seq = excluded.message_seq,
              source_file = excluded.source_file,
              line_no = excluded.line_no
            """,
            (
                message.session_id,
                message.source_uuid,
                message.round,
                message.message_seq,
                message.source_file,
                message.line_no,
            ),
        )
        if cursor.rowcount:
            session_ids.add(message.session_id)
    refresh_session_forks(conn)
    conn.commit()
    return sorted(session_ids)


def refresh_session_forks(conn: sqlite3.Connection, *, min_shared_turns: int | None = None) -> int:
    """Infer Claude transcript forks from source_uuid overlap across sessions.

    Claude can create a new session file that copies prior transcript rows when
    a message is edited/retried or an intentional fork is made. The copied rows
    keep their source_uuid, so overlap is a strong fork signal.
    """
    if min_shared_turns is None:
        min_shared_turns = load_settings().get("ingest", {}).get("fork_min_shared_turns", 6)
    stats = {
        r["session_id"]: dict(r)
        for r in conn.execute(
            """
            SELECT session_id,
                   COUNT(*) AS turn_count,
                   MAX(round) AS max_round,
                   MIN(created_at) AS first_ingested
            FROM turns
            GROUP BY session_id
            """
        ).fetchall()
    }
    pairs = conn.execute(
        """
        SELECT a.session_id AS s1,
               b.session_id AS s2,
               COUNT(*) AS shared_turns,
               MAX(a.round) AS s1_fork_round,
               MAX(b.round) AS s2_fork_round
        FROM turns a
        JOIN turns b ON b.source_uuid = a.source_uuid
                    AND b.session_id > a.session_id
        GROUP BY a.session_id, b.session_id
        HAVING shared_turns >= ?
        """,
        (min_shared_turns,),
    ).fetchall()

    best: dict[str, dict] = {}
    for row in pairs:
        s1, s2 = row["s1"], row["s2"]
        st1, st2 = stats.get(s1), stats.get(s2)
        if not st1 or not st2:
            continue
        # Later-ingested occurrence is the fork child. Ties are rare; keep the
        # ordering deterministic so the table remains stable after rebuilds.
        if (st1["first_ingested"], s1) > (st2["first_ingested"], s2):
            child, parent = s1, s2
            child_round, parent_round = row["s1_fork_round"], row["s2_fork_round"]
            child_stats, parent_stats = st1, st2
        else:
            child, parent = s2, s1
            child_round, parent_round = row["s2_fork_round"], row["s1_fork_round"]
            child_stats, parent_stats = st2, st1

        candidate = {
            "child_session_id": child,
            "parent_session_id": parent,
            "fork_round": child_round,
            "parent_fork_round": parent_round,
            "delta_start_round": (child_round or 0) + 1,
            "shared_turns": row["shared_turns"],
            "child_turns": child_stats["turn_count"],
            "parent_turns": parent_stats["turn_count"],
            "child_shared_ratio": row["shared_turns"] / max(child_stats["turn_count"], 1),
        }
        prev = best.get(child)
        if prev is None or (
            candidate["shared_turns"],
            candidate["parent_turns"],
            candidate["parent_session_id"],
        ) > (
            prev["shared_turns"],
            prev["parent_turns"],
            prev["parent_session_id"],
        ):
            best[child] = candidate

    conn.execute("DELETE FROM session_forks")
    for item in best.values():
        conn.execute(
            """
            INSERT INTO session_forks
            (child_session_id, parent_session_id, fork_round, parent_fork_round,
             delta_start_round, shared_turns, child_turns, parent_turns,
             child_shared_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["child_session_id"],
                item["parent_session_id"],
                item["fork_round"],
                item["parent_fork_round"],
                item["delta_start_round"],
                item["shared_turns"],
                item["child_turns"],
                item["parent_turns"],
                item["child_shared_ratio"],
            ),
        )
    return len(best)


def get_session_fork(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM session_forks WHERE child_session_id = ?",
        (session_id,),
    ).fetchone()


def reconcile_deleted_files(conn: sqlite3.Connection, project_dirs: tuple[Path, ...]) -> int:
    """Delete turns whose source JSONL file no longer exists on disk.

    Only checks files under project_dirs (room JSONL); export and other
    sources are left alone. Returns the number of turns removed.
    """
    prefixes = [str(d) for d in project_dirs]
    rows = conn.execute("SELECT DISTINCT source_file FROM turns").fetchall()
    gone = [
        r["source_file"]
        for r in rows
        if r["source_file"]
        and any(r["source_file"].startswith(p) for p in prefixes)
        and not Path(r["source_file"]).exists()
    ]
    if not gone:
        return 0
    ph = ",".join("?" for _ in gone)
    cur = conn.execute(f"DELETE FROM turns WHERE source_file IN ({ph})", gone)
    removed = cur.rowcount
    conn.execute(
        "DELETE FROM messages WHERE source_uuid NOT IN (SELECT source_uuid FROM turns)"
    )
    conn.commit()
    return removed


def get_session_ids(conn: sqlite3.Connection) -> list[str]:
    return [row["session_id"] for row in conn.execute("SELECT DISTINCT session_id FROM turns ORDER BY session_id").fetchall()]


def room_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    """从 session 的 turns 落在哪个房间 project_dir 推断房间。

    一个 session 的 turns 可能散落在同一房间的多个文件（fork/撤回会拆分），但都在同一
    房间下，所以按 turn 数取多数的房间即可。全落在房间外（导出等）时返回 None。
    """
    rows = conn.execute(
        "SELECT source_file, COUNT(*) AS n FROM turns WHERE session_id = ? GROUP BY source_file",
        (session_id,),
    ).fetchall()
    tally: dict[str, int] = {}
    for r in rows:
        room = room_for_source_file(r["source_file"])
        if room:
            tally[room] = tally.get(room, 0) + r["n"]
    if not tally:
        return None
    return max(tally, key=tally.get)


def _distinct_in(conn: sqlite3.Connection, select_col: str, where_col: str, values) -> set[str]:
    """SELECT DISTINCT select_col FROM turns WHERE where_col IN (values)，分块防变量上限。"""
    out: set[str] = set()
    vals = list(values)
    for i in range(0, len(vals), 500):
        chunk = vals[i : i + 500]
        ph = ",".join("?" for _ in chunk)
        out.update(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {select_col} FROM turns WHERE {where_col} IN ({ph})",
                chunk,
            ).fetchall()
        )
    return out


def sessions_in_files(conn: sqlite3.Connection, source_files) -> set[str]:
    """这些 source_file 在库里对应到哪些 session_id。"""
    return _distinct_in(conn, "session_id", "source_file", source_files)


def files_for_sessions(conn: sqlite3.Connection, session_ids) -> set[str]:
    """这些 session_id 的 turns 在库里散落于哪些 source_file。"""
    return _distinct_in(conn, "source_file", "session_id", session_ids)


def _jieba_seg(text: str) -> str:
    return " ".join(jieba.lcut(text)) if text else ""


def _has_share_expr(alias: str = "c") -> str:
    return f"NULLIF(TRIM(COALESCE({alias}.share, '')), '') IS NOT NULL"


def card_visible_clause(viewer: str | None, alias: str = "c") -> tuple[str, list[str]]:
    """卡可见性谓词（room viewer 隐私模型）：本房卡全可见，他房卡仅在有 share 时可见。

    存储层的公共谓词——retrieval / context / treesnap / 工作台导出共用同一条规则，
    隐私规则今后只改这一处。返回 (SQL 片段, 参数列表)，viewer=None 表示不过滤。
    alias 是 cards 表在查询里的别名（或表名，如内联查询用 "cards"）。
    """
    room = ROOM_SLUGS.get(viewer, viewer) if viewer else None
    if room is None:
        return "1=1", []
    return f"({alias}.room = ? OR {_has_share_expr(alias)})", [room]


def insert_card(
    conn: sqlite3.Connection,
    card: dict,
    *,
    label: str = "",
    source_file: str = "",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO cards
        (card_id, session_id, turn_start, turn_end, theme, share, private, timestamp, room, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card["card_id"],
            card.get("session_id", ""),
            card.get("turns", [None, None])[0],
            card.get("turns", [None, None])[1],
            card.get("theme", ""),
            card.get("share", ""),
            card.get("private", ""),
            card.get("timestamp"),
            card.get("room", DEFAULT_ROOM),
            card.get("model"),
        ),
    )
    for tag in card.get("tags") or []:
        tag = tag.strip().lower()
        if tag:
            conn.execute(
                "INSERT OR IGNORE INTO card_tags (card_id, tag) VALUES (?, ?)",
                (card["card_id"], tag),
            )
    conn.execute(
        """
        INSERT OR REPLACE INTO card_raw (card_id, raw, label, source_file)
        VALUES (?, ?, ?, ?)
        """,
        (card["card_id"], card.get("raw", ""), label, source_file),
    )
    conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card["card_id"],))
    conn.execute(
        "INSERT INTO cards_fts (card_id, theme, share, private) VALUES (?, ?, ?, ?)",
        (
            card["card_id"],
            _jieba_seg(card.get("theme", "")),
            _jieba_seg(card.get("share", "")),
            _jieba_seg(card.get("private", "")),
        ),
    )


def delete_card(conn: sqlite3.Connection, card_id: str) -> None:
    conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))


def get_session_cards(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cards WHERE session_id = ? ORDER BY turn_start",
        (session_id,),
    ).fetchall()


def search_cards(conn: sqlite3.Connection, query: str, viewer: str) -> list[sqlite3.Row]:
    tokens = _jieba_seg(query)
    if not tokens.strip():
        return []
    room = ROOM_SLUGS.get(viewer, viewer)
    rows = conn.execute(
        """
        SELECT c.*, bm25(cards_fts) AS rank
        FROM cards_fts f
        JOIN cards c ON c.card_id = f.card_id
        WHERE cards_fts MATCH ?
          AND (c.room = ? OR 1)
        ORDER BY rank
        LIMIT 40
        """,
        (tokens, room),
    ).fetchall()
    return rows


def search_cards_time(
    conn: sqlite3.Connection, start: str, end: str, viewer: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM cards
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 40
        """,
        (start, end),
    ).fetchall()


def load_turns_from_round(conn: sqlite3.Connection, session_id: str, start_round: int) -> list[Message]:
    rows = conn.execute(
        """
        SELECT t.round, t.message_seq, t.line_no, t.source_file, t.source_uuid,
               m.role, m.speaker, m.text, m.timestamp, m.parent_uuid, m.source,
               m.model, m.has_image, m.image_count
        FROM turns t
        JOIN messages m ON m.source_uuid = t.source_uuid
        WHERE t.session_id = ? AND t.round >= ?
        ORDER BY t.round ASC, t.message_seq ASC, t.line_no ASC
        """,
        (session_id, start_round),
    ).fetchall()
    messages: list[Message] = []
    for idx, row in enumerate(rows, start=1):
        side = "L" if row["role"] == "user" else "C"
        messages.append(
            Message(
                role=row["role"],
                speaker=row["speaker"],
                text=row["text"],
                timestamp=row["timestamp"] or "",
                session_id=session_id,
                seq=idx,
                round=row["round"],
                label=f"{row['round']:02d}{side}",
                source_file=row["source_file"] or "",
                source=row["source"] or "opus-legacy",
                source_uuid=row["source_uuid"],
                parent_uuid=row["parent_uuid"] or "",
                message_seq=row["message_seq"],
                line_no=row["line_no"] or 0,
                has_image=row["has_image"] or 0,
                image_count=row["image_count"] or 0,
                model=row["model"] or "",
            )
        )
    return messages
