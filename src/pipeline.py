"""事件卡管线：从 turns 生成事件卡。"""
from __future__ import annotations

import datetime as dt
import sqlite3

from config import DEFAULT_ROOM, ROOMS, load_settings
from context import rebuild_context
from db import get_session_cards, room_for_session
from gen_cards import process_session_cards, update_session_cards
from model import ModelRunner


def process_session(
    conn: sqlite3.Connection,
    run_id: str,
    session_id: str,
    model: ModelRunner,
    room: str | None = None,
) -> bool:
    existing = get_session_cards(conn, session_id)
    if existing:
        return False
    resolved = room or room_for_session(conn, session_id) or DEFAULT_ROOM
    process_session_cards(conn, run_id, session_id, model, room=resolved)
    return True


def rebuild_index(conn: sqlite3.Connection) -> dict:
    """Rebuild the derived card index according to settings.

    The v2 production path is the Leiden hierarchy:
    build_graph -> build_hierarchy -> store_hierarchy.
    """
    s = load_settings().get("index", {})
    if not s.get("enabled", True):
        return {"status": "skipped", "reason": "settings.index.enabled=false"}

    algorithm = s.get("algorithm", "leiden")
    if algorithm in ("none", "off", "disabled"):
        return {"status": "skipped", "reason": f"settings.index.algorithm={algorithm}"}
    if algorithm != "leiden":
        raise ValueError(f"unknown index algorithm: {algorithm!r}")

    card_count = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    if card_count == 0:
        return {"status": "skipped", "reason": "no cards"}

    # Fold new tags into existing entities before building the graph, so new
    # synonyms don't refragment the index. Fail-soft: if the LLM judge is
    # unavailable the unresolved tags just stay out of the entity edges.
    resolve_stats = {"status": "skipped"}
    if s.get("resolve_entities", True):
        try:
            from entity_resolve import resolve_new_tags
            resolve_stats = resolve_new_tags(conn)
        except Exception as e:  # noqa: BLE001 -- index rebuild must not die on judge errors
            resolve_stats = {"status": "resolve_failed", "error": str(e)[:200]}

    from graph import build_graph
    from community import build_hierarchy, store_hierarchy

    ids, edges = build_graph(conn, k=s.get("knn_k", 12))
    forest = build_hierarchy(ids, edges)
    stats = store_hierarchy(conn, ids, forest, edges)
    return {"status": "rebuilt", "resolve": resolve_stats, **stats}


def finalize_card_updates(conn: sqlite3.Connection, viewer: str | None = None) -> dict:
    """Refresh derived index and context after cards change."""
    index_stats = {"status": "skipped", "reason": "settings.index.rebuild_after_cards=false"}
    if load_settings().get("index", {}).get("rebuild_after_cards", True):
        index_stats = rebuild_index(conn)
    rebuild_context(conn, viewer=viewer)
    return index_stats


def reroom_cards(conn: sqlite3.Connection) -> dict:
    """把每张卡的 room 校正为其 session 来源派生出的房间。

    v2 adapter 以 source_sessions.room（本机 policy 结果）为权威；legacy 来源再按
    source_file 所在的房间 project_dir 推断。历史上新 session 首次
    出卡曾一律回退默认房间，导致其他房间内容被误标；这里按 session 重新派生并批量改正。
    来源落在房间外（导出等）无法派生的卡保持不动。返回改动统计。
    """
    session_ids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM cards"
    ).fetchall()]
    changed = 0
    moves: dict[str, int] = {}
    for session_id in session_ids:
        derived = room_for_session(conn, session_id)
        if not derived:
            continue
        cur = conn.execute(
            "UPDATE cards SET room = ? WHERE session_id = ? AND room != ?",
            (derived, session_id, derived),
        )
        if cur.rowcount:
            changed += cur.rowcount
            moves[f"->{derived}"] = moves.get(f"->{derived}", 0) + cur.rowcount
    if changed:
        conn.commit()
    return {"cards_rerooted": changed, "by_target": moves}


def check_card_gen_threshold(conn: sqlite3.Connection) -> tuple[bool, str]:
    """检查是否满足出卡条件：新轮数 >= min 且距上次出卡 >= min 分钟。"""
    s = load_settings().get("card_gen", {})
    min_rounds = s.get("min_new_turns", 5)
    min_minutes = s.get("min_interval_minutes", 60)

    attempt_row = conn.execute(
        """
        SELECT MAX(created_at) AS last_attempt FROM model_calls
        WHERE step IN ('gen_cards', 'gen_cards_update',
                       'gen_cards_attempt', 'gen_cards_attempt_error')
        """
    ).fetchone()
    last_attempt = attempt_row["last_attempt"] if attempt_row and attempt_row["last_attempt"] else None
    elapsed = float("inf")

    if last_attempt:
        try:
            last_attempt_dt = dt.datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
            if last_attempt_dt.tzinfo is None:
                last_attempt_dt = last_attempt_dt.replace(tzinfo=dt.UTC)
        except ValueError:
            last_attempt_dt = None

        if last_attempt_dt:
            elapsed = (dt.datetime.now(dt.UTC) - last_attempt_dt).total_seconds() / 60
            if elapsed < min_minutes:
                return False, f"too soon ({elapsed:.0f}min < {min_minutes}min)"

    success_row = conn.execute(
        """
        SELECT MAX(created_at) AS last_success FROM model_calls
        WHERE step IN ('gen_cards', 'gen_cards_update')
        """
    ).fetchone()
    last_success = success_row["last_success"] if success_row and success_row["last_success"] else None
    if last_success:
        new_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM (
              SELECT DISTINCT session_id, round
              FROM turns
              WHERE created_at > ? AND is_context = 0
            )
            """,
            (last_success,),
        ).fetchone()["n"]
    else:
        new_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM (
              SELECT DISTINCT session_id, round
              FROM turns
              WHERE is_context = 0
            )
            """
        ).fetchone()["n"]

    if new_count < min_rounds:
        return False, f"only {new_count} new rounds (need {min_rounds})"

    return True, f"{new_count} new rounds, {elapsed:.0f}min elapsed"


def sessions_needing_update(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """找出有新 turns 超过最后一张卡的 session。按最近活跃排序，返回 [(session_id, room), ...]"""
    s = load_settings().get("card_gen", {})
    min_first_session_turns = s.get("min_first_session_turns", 3)
    raw_exempt_globs = s.get("min_first_session_turns_exempt_source_globs", [])
    if isinstance(raw_exempt_globs, str):
        raw_exempt_globs = [raw_exempt_globs]
    exempt_globs = [
        pattern for pattern in raw_exempt_globs
        if isinstance(pattern, str) and pattern
    ] if isinstance(raw_exempt_globs, list) else []
    raw_exempt_sources = s.get("min_first_session_turns_exempt_sources", [])
    if isinstance(raw_exempt_sources, str):
        raw_exempt_sources = [raw_exempt_sources]
    exempt_sources = {
        source for source in raw_exempt_sources
        if isinstance(source, str) and source
    } if isinstance(raw_exempt_sources, list) else set()
    exempt_clause = ""
    if exempt_globs:
        matches = " OR ".join("et.source_file GLOB ?" for _ in exempt_globs)
        exempt_clause = f"""
            OR EXISTS (
              SELECT 1
              FROM turns et
              WHERE et.session_id = t.session_id
                AND ({matches})
            )
        """

    rows = conn.execute(f"""
        WITH turn_stats AS (
            SELECT session_id,
                   MAX(round) as max_round,
                   COUNT(*) as turn_count,
                   COUNT(DISTINCT round) as round_count,
                   MAX(created_at) as last_activity
            FROM turns
            GROUP BY session_id
        ),
        card_stats AS (
            SELECT session_id,
                   MAX(turn_end) as last_card_end,
                   MAX(room) as card_room,
                   COUNT(*) as card_count
            FROM cards
            GROUP BY session_id
        ),
        fork_delta AS (
            SELECT t.session_id,
                   COUNT(*) as delta_turn_count,
                   COUNT(DISTINCT t.round) as delta_round_count
            FROM turns t
            JOIN session_forks f ON f.child_session_id = t.session_id
            WHERE t.round > f.fork_round
            GROUP BY t.session_id
        )
        SELECT t.session_id, t.max_round, t.turn_count, t.last_activity,
               c.last_card_end,
               COALESCE(c.card_room, pc.card_room) as card_room,
               COALESCE(c.card_count, 0) as card_count,
               f.fork_round,
               CASE
                 WHEN f.child_session_id IS NOT NULL THEN COALESCE(fd.delta_round_count, 0)
                 ELSE t.round_count
               END as effective_round_count
        FROM turn_stats t
        LEFT JOIN card_stats c ON c.session_id = t.session_id
        LEFT JOIN session_forks f ON f.child_session_id = t.session_id
        LEFT JOIN fork_delta fd ON fd.session_id = t.session_id
        LEFT JOIN card_stats pc ON pc.session_id = f.parent_session_id
        WHERE t.max_round > COALESCE(c.last_card_end, f.fork_round, 0)
          AND NOT EXISTS (
            SELECT 1 FROM conversation_card_branches tree_branch
            WHERE tree_branch.session_id = t.session_id
          )
          AND (
            COALESCE(c.card_count, 0) > 0
            OR CASE
                 WHEN f.child_session_id IS NOT NULL THEN COALESCE(fd.delta_round_count, 0)
                 ELSE t.round_count
               END >= ?
            {exempt_clause}
          )
          AND NOT (
            COALESCE(c.card_count, 0) = 0
            AND EXISTS (
              SELECT 1
              FROM session_forks child_fork
              JOIN cards child_card
                ON child_card.session_id = child_fork.child_session_id
              WHERE child_fork.parent_session_id = t.session_id
                AND child_card.turn_start <= child_fork.fork_round
            )
          )
        ORDER BY last_activity DESC
    """, (min_first_session_turns, *exempt_globs)).fetchall()
    ranked_results: list[tuple[str, str, str]] = []
    for r in rows:
        # v2 adapter 的本机 policy 结果优先；legacy 才从 source_file/project_dir 派生。
        # 能派生就用它——
        # 这样早期误判成默认房间的其他房间 session 会在下次出卡时自愈。只有来源落在房间外
        # （历史导出等）无法派生时，才回退已有卡的房间标签，再不行才用默认房间。
        room = room_for_session(conn, r["session_id"]) or (
            r["card_room"] if r["card_room"] in ROOMS else DEFAULT_ROOM
        )
        ranked_results.append((r["session_id"], room, r["last_activity"] or ""))

    tree_rows = conn.execute(
        """
        SELECT branch.session_id, branch.room, branch.source,
               COUNT(DISTINCT CASE WHEN t.is_context = 0 THEN t.round END) AS owned_rounds,
               COUNT(DISTINCT own_card.card_id) AS card_count,
               MAX(t.created_at) AS last_activity,
               SUM(CASE WHEN t.is_context = 0 AND covered.node_id IS NULL THEN 1 ELSE 0 END)
                 AS uncovered_nodes
        FROM conversation_card_branches branch
        JOIN turns t ON t.session_id = branch.session_id
        LEFT JOIN card_nodes covered ON covered.node_id = t.source_uuid
        LEFT JOIN cards own_card ON own_card.session_id = branch.session_id
        GROUP BY branch.session_id, branch.room, branch.source
        HAVING uncovered_nodes > 0
        """
    ).fetchall()
    for row in tree_rows:
        if (
            row["card_count"] > 0
            or row["owned_rounds"] >= min_first_session_turns
            or row["source"] in exempt_sources
        ):
            ranked_results.append(
                (row["session_id"], row["room"], row["last_activity"] or "")
            )

    ranked_results.sort(key=lambda item: (item[2], item[0]), reverse=True)
    return [(session_id, room) for session_id, room, _activity in ranked_results]


def auto_generate_cards(
    conn: sqlite3.Connection,
    run_id: str,
    model: ModelRunner,
) -> int:
    """双阈值触发：检查条件 → 找需要更新的 session → 增量出卡 → 刷 last-24。返回处理的 session 数。"""
    ok, reason = check_card_gen_threshold(conn)
    if not ok:
        return 0

    s = load_settings().get("card_gen", {})
    max_per_trigger = s.get("max_sessions_per_trigger", 3)

    targets = sessions_needing_update(conn)
    if not targets:
        return 0

    targets = targets[:max_per_trigger]

    count = 0
    failures: list[tuple[str, Exception]] = []
    for session_id, room in targets:
        try:
            update_session_cards(conn, run_id, session_id, model, room=room)
            count += 1
        except Exception as exc:
            conn.rollback()
            failures.append((session_id, exc))
            print(f"  auto-cards FAILED {session_id}: {exc}")

    if count:
        finalize_card_updates(conn)

    if failures:
        raise RuntimeError(
            f"auto-cards failed for {len(failures)}/{len(targets)} sessions; "
            "see preceding FAILED lines"
        )

    return count
