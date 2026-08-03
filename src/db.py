from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from config import DEFAULT_ROOM, MEMORY, ROOMS, ROOM_SLUGS, load_settings, room_for_source_file
from memory_types import ConversationNode, ConversationTreeBatch, Message

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
if VENDOR.exists() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
import jieba  # type: ignore

if hasattr(jieba, "setLogLevel"):  # 工作台的 jieba 兜底 mock 没有这方法
    jieba.setLogLevel(60)  # 静音 "Building prefix dict..."——检索是 agent 在用，噪音会混进每次输出

SCHEMA_VERSION = 13

DB = MEMORY / "data" / "fragments.db"
SCHEMA = MEMORY / "config" / "schema.sql"


def connect(db_path: Path = DB) -> sqlite3.Connection:
    """打开数据库。全新文件按 schema.sql 建表；已有库只校验版本，不做运行时迁移。

    版本不符时报错并指向 migrations/——升级是显式的一次性操作（编号 SQL），
    不是 connect() 里的隐式考古。迁移规则与 runbook 见 skills/ops/common.md「schema 迁移」。
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
        native_message_id = getattr(message, "native_message_id", "") or ""
        if native_message_id:
            _upsert_source_session(conn, message, source)
            _assert_immutable_native_message(conn, message, source)
        conn.execute(
            """
            INSERT INTO messages
            (source_uuid, role, speaker, text, timestamp, parent_uuid, source,
             provider, model, native_message_id, native_parent_message_id,
             has_image, image_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                message.provider or None,
                message.model or None,
                native_message_id or None,
                message.native_parent_message_id or None,
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


def ingest_conversation_tree(
    conn: sqlite3.Connection,
    batch: ConversationTreeBatch,
    *,
    project_cards: bool = True,
) -> list[str]:
    """Persist one additive tree batch and refresh its Card-only branch projection.

    Cursor/head observations are inserted after the immutable nodes and never
    create turns. Only portable message nodes participate in the Card projection.
    """
    ordered = _validate_and_order_tree_batch(conn, batch)
    changed_branches: set[str] = set()
    conn.execute("SAVEPOINT ingest_conversation_tree")
    try:
        for node in ordered:
            _insert_or_assert_tree_node(conn, node)
        if project_cards:
            for node in ordered:
                branch_id, newly_assigned = _assign_tree_branch(conn, node)
                if newly_assigned:
                    changed_branches.add(branch_id)
                if node.kind == "message":
                    _insert_tree_message(conn, node)
            for branch_id in sorted(changed_branches):
                _rebuild_tree_branch_turns(conn, branch_id)
        for observation in batch.observations:
            node = conn.execute(
                "SELECT source, room FROM conversation_nodes WHERE node_id = ?",
                (observation.node_id,),
            ).fetchone()
            if node is None or node["source"] != batch.source or node["room"] != batch.room:
                raise ValueError(
                    f"observation {observation.observation_id!r} points outside "
                    f"source={batch.source!r}, room={batch.room!r}"
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_observations
                (observation_id, source, room, native_context_id, kind,
                 observed_at, node_id, native_node_id, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.source,
                    observation.room,
                    observation.native_context_id,
                    observation.kind,
                    observation.observed_at,
                    observation.node_id,
                    observation.native_node_id,
                    observation.payload_json,
                ),
            )
        if project_cards:
            refresh_session_forks(conn)
        conn.execute("RELEASE SAVEPOINT ingest_conversation_tree")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ingest_conversation_tree")
        conn.execute("RELEASE SAVEPOINT ingest_conversation_tree")
        raise
    return sorted(changed_branches)


def _validate_and_order_tree_batch(
    conn: sqlite3.Connection, batch: ConversationTreeBatch
) -> list[ConversationNode]:
    incoming = {node.node_id: node for node in batch.nodes}
    if len(incoming) != len(batch.nodes):
        raise ValueError("conversation tree batch contains duplicate canonical node IDs")
    existing_rows = conn.execute(
        """
        SELECT node_id, parent_node_id, source, room
        FROM conversation_nodes
        WHERE source = ? AND room = ?
        """,
        (batch.source, batch.room),
    ).fetchall()
    parents = {row["node_id"]: row["parent_node_id"] for row in existing_rows}
    for node in batch.nodes:
        if node.source != batch.source or node.room != batch.room:
            raise ValueError("tree node source/room must match its envelope")
        if node.parent_node_id and node.parent_node_id not in incoming and node.parent_node_id not in parents:
            outside = conn.execute(
                "SELECT source, room FROM conversation_nodes WHERE node_id = ?",
                (node.parent_node_id,),
            ).fetchone()
            if outside is not None:
                raise ValueError(
                    f"parent {node.native_parent_node_id!r} belongs to a different source/room"
                )
            raise ValueError(
                f"missing parent {node.native_parent_node_id!r} for node {node.native_node_id!r}"
            )
        parents[node.node_id] = node.parent_node_id

    state: dict[str, int] = {}
    ordered: list[ConversationNode] = []

    def visit(node_id: str) -> None:
        status = state.get(node_id, 0)
        if status == 2:
            return
        if status == 1:
            raise ValueError(f"conversation tree contains a parent cycle at {node_id}")
        state[node_id] = 1
        parent_id = parents.get(node_id)
        if parent_id in incoming:
            visit(parent_id)
        state[node_id] = 2
        if node_id in incoming:
            ordered.append(incoming[node_id])

    for node in sorted(
        batch.nodes,
        key=lambda item: (item.occurred_at or "", item.node_id),
    ):
        visit(node.node_id)
    return ordered


def _insert_or_assert_tree_node(
    conn: sqlite3.Connection, node: ConversationNode
) -> None:
    existing = conn.execute(
        "SELECT * FROM conversation_nodes WHERE node_id = ?", (node.node_id,)
    ).fetchone()
    values = {
        "source": node.source,
        "room": node.room,
        "native_node_id": node.native_node_id,
        "parent_node_id": node.parent_node_id,
        "native_parent_node_id": node.native_parent_node_id,
        "occurred_at": node.occurred_at,
        "kind": node.kind,
        "source_type": node.source_type,
        "role": node.role,
        "text": node.text,
        "message_json": node.message_json,
        "provider": node.provider,
        "model": node.model,
    }
    if existing is not None:
        conflicts = [key for key, value in values.items() if existing[key] != value]
        if conflicts:
            raise ValueError(
                f"immutable conversation node conflict for {node.native_node_id!r}: "
                + ", ".join(conflicts)
            )
        return
    conn.execute(
        """
        INSERT INTO conversation_nodes
        (node_id, source, room, native_node_id, parent_node_id,
         native_parent_node_id, occurred_at, kind, source_type, role, text,
         message_json, provider, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node.node_id, node.source, node.room, node.native_node_id,
            node.parent_node_id, node.native_parent_node_id, node.occurred_at,
            node.kind, node.source_type, node.role, node.text,
            node.message_json, node.provider, node.model,
        ),
    )


def _tree_branch_id(node_id: str) -> str:
    return hashlib.sha256(f"card-branch|{node_id}".encode("utf-8")).hexdigest()[:32]


def _assign_tree_branch(
    conn: sqlite3.Connection, node: ConversationNode
) -> tuple[str, bool]:
    existing = conn.execute(
        "SELECT session_id FROM conversation_node_branches WHERE node_id = ?",
        (node.node_id,),
    ).fetchone()
    if existing is not None:
        return existing["session_id"], False

    parent_branch: str | None = None
    if node.parent_node_id:
        parent = conn.execute(
            "SELECT session_id FROM conversation_node_branches WHERE node_id = ?",
            (node.parent_node_id,),
        ).fetchone()
        if parent is None:
            raise ValueError(
                f"parent branch missing for conversation node {node.native_node_id!r}"
            )
        parent_branch = parent["session_id"]

    branch_id = parent_branch
    if parent_branch is not None:
        inherited_child = conn.execute(
            """
            SELECT 1
            FROM conversation_nodes child
            JOIN conversation_node_branches owned ON owned.node_id = child.node_id
            WHERE child.parent_node_id = ? AND owned.session_id = ?
            LIMIT 1
            """,
            (node.parent_node_id, parent_branch),
        ).fetchone()
        if inherited_child is not None:
            branch_id = None

    if branch_id is None or parent_branch is None:
        branch_id = _tree_branch_id(node.node_id)
        conn.execute(
            """
            INSERT INTO conversation_card_branches
            (session_id, source, room, anchor_node_id, parent_session_id, fork_node_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                branch_id, node.source, node.room, node.node_id,
                parent_branch, node.parent_node_id if parent_branch else None,
            ),
        )
    conn.execute(
        "INSERT INTO conversation_node_branches (node_id, session_id) VALUES (?, ?)",
        (node.node_id, branch_id),
    )
    return branch_id, True


def _insert_tree_message(conn: sqlite3.Connection, node: ConversationNode) -> None:
    existing = conn.execute(
        """
        SELECT role, text, timestamp, parent_uuid, source, provider, model
        FROM messages WHERE source_uuid = ?
        """,
        (node.node_id,),
    ).fetchone()
    expected = (
        node.role, node.text, node.occurred_at, node.parent_node_id,
        node.source, node.provider, node.model,
    )
    if existing is not None:
        actual = tuple(existing[key] for key in (
            "role", "text", "timestamp", "parent_uuid", "source", "provider", "model"
        ))
        if actual != expected:
            raise ValueError(f"immutable Card message projection conflict for {node.native_node_id!r}")
        return
    conn.execute(
        """
        INSERT INTO messages
        (source_uuid, role, speaker, text, timestamp, parent_uuid, source,
         provider, model, has_image, image_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """,
        (
            node.node_id,
            node.role,
            user_name_for_role(node.role),
            node.text,
            node.occurred_at,
            node.parent_node_id,
            node.source,
            node.provider,
            node.model,
        ),
    )


def user_name_for_role(role: str | None) -> str:
    from config import agent_name, user_name
    return user_name() if role == "user" else agent_name()


def _rebuild_tree_branch_turns(conn: sqlite3.Connection, session_id: str) -> None:
    tips = conn.execute(
        """
        SELECT n.node_id
        FROM conversation_nodes n
        JOIN conversation_node_branches owned ON owned.node_id = n.node_id
        WHERE owned.session_id = ?
          AND NOT EXISTS (
            SELECT 1
            FROM conversation_nodes child
            JOIN conversation_node_branches child_owned
              ON child_owned.node_id = child.node_id
            WHERE child.parent_node_id = n.node_id
              AND child_owned.session_id = owned.session_id
          )
        """,
        (session_id,),
    ).fetchall()
    if len(tips) != 1:
        raise ValueError(
            f"Card branch {session_id} must have one owned tip, found {len(tips)}"
        )
    path: list[sqlite3.Row] = []
    node_id: str | None = tips[0]["node_id"]
    seen: set[str] = set()
    while node_id:
        if node_id in seen:
            raise ValueError(f"conversation tree cycle while projecting {session_id}")
        seen.add(node_id)
        row = conn.execute(
            """
            SELECT n.*, owned.session_id AS owner_session_id
            FROM conversation_nodes n
            JOIN conversation_node_branches owned ON owned.node_id = n.node_id
            WHERE n.node_id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"missing conversation node {node_id} during Card projection")
        path.append(row)
        node_id = row["parent_node_id"]
    path.reverse()

    round_no = 0
    message_seq = 0
    line_no = 0
    for node in path:
        if node["kind"] != "message":
            continue
        line_no += 1
        if node["role"] == "user" or round_no == 0:
            round_no += 1
            message_seq = 0
        message_seq += 1
        conn.execute(
            """
            INSERT INTO turns
            (session_id, source_uuid, round, message_seq, source_file, line_no, is_context)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(session_id, source_uuid) DO UPDATE SET
              round = excluded.round,
              message_seq = excluded.message_seq,
              source_file = excluded.source_file,
              line_no = excluded.line_no,
              is_context = excluded.is_context
            """,
            (
                session_id,
                node["node_id"],
                round_no,
                message_seq,
                line_no,
                0 if node["owner_session_id"] == session_id else 1,
            ),
        )


def _upsert_source_session(
    conn: sqlite3.Connection, message: Message, source: str
) -> None:
    if not message.native_session_id or not message.source_route or not message.room:
        raise ValueError(
            "canonical adapter messages require native_session_id, source_route, and resolved room"
        )
    conn.execute(
        """
        INSERT INTO source_sessions
        (session_id, source, native_session_id, native_parent_session_id,
         parent_session_id, source_route, room)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          native_parent_session_id = excluded.native_parent_session_id,
          parent_session_id = excluded.parent_session_id,
          source_route = excluded.source_route,
          room = excluded.room,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            message.session_id,
            source,
            message.native_session_id,
            message.native_parent_session_id or None,
            message.parent_session_id or None,
            message.source_route,
            message.room,
        ),
    )


def _assert_immutable_native_message(
    conn: sqlite3.Connection, message: Message, source: str
) -> None:
    existing = conn.execute(
        """
        SELECT role, text, timestamp, parent_uuid, source,
               native_message_id, native_parent_message_id
        FROM messages WHERE source_uuid = ?
        """,
        (message.source_uuid,),
    ).fetchone()
    if existing is None:
        return
    expected = {
        "role": message.role,
        "text": message.text,
        "timestamp": message.timestamp or None,
        "parent_uuid": message.parent_uuid,
        "source": source,
        "native_message_id": message.native_message_id,
        "native_parent_message_id": message.native_parent_message_id or None,
    }
    conflicts = [
        key for key, value in expected.items()
        if existing[key] != value
    ]
    if conflicts:
        raise ValueError(
            f"immutable native message conflict for {message.native_message_id!r}: "
            + ", ".join(conflicts)
        )


def refresh_session_forks(conn: sqlite3.Connection, *, min_shared_turns: int | None = None) -> int:
    """Infer Claude transcript forks from source_uuid overlap across sessions.

    Claude can create a new session file that copies prior transcript rows when
    a message is edited/retried or an intentional fork is made. The copied rows
    keep their source_uuid, so overlap is a strong fork signal.
    """
    if min_shared_turns is None:
        min_shared_turns = load_settings().get("ingest", {}).get("fork_min_shared_turns", 2)
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

    # Tree adapters already provide the parent edge. Their Card sessions are a
    # derived stream decomposition, so preserve that explicit relationship even
    # when a branch has fewer shared turns than the legacy overlap threshold.
    tree_branches = conn.execute(
        """
        SELECT session_id, parent_session_id
        FROM conversation_card_branches
        WHERE parent_session_id IS NOT NULL
        """
    ).fetchall()
    for branch in tree_branches:
        child = branch["session_id"]
        parent = branch["parent_session_id"]
        child_stats = stats.get(child)
        parent_stats = stats.get(parent)
        if not child_stats or not parent_stats:
            continue
        shared = conn.execute(
            """
            SELECT COUNT(*) AS shared_turns,
                   MAX(ct.round) AS child_fork_round,
                   MAX(pt.round) AS parent_fork_round
            FROM turns ct
            JOIN turns pt ON pt.source_uuid = ct.source_uuid
                         AND pt.session_id = ?
            WHERE ct.session_id = ? AND ct.is_context = 1
            """,
            (parent, child),
        ).fetchone()
        delta = conn.execute(
            """
            SELECT MIN(round) AS delta_start_round
            FROM turns WHERE session_id = ? AND is_context = 0
            """,
            (child,),
        ).fetchone()
        shared_turns = int(shared["shared_turns"] or 0)
        best[child] = {
            "child_session_id": child,
            "parent_session_id": parent,
            "fork_round": int(shared["child_fork_round"] or 0),
            "parent_fork_round": int(shared["parent_fork_round"] or 0),
            "delta_start_round": int(delta["delta_start_round"] or 1),
            "shared_turns": shared_turns,
            "child_turns": child_stats["turn_count"],
            "parent_turns": parent_stats["turn_count"],
            "child_shared_ratio": shared_turns / max(child_stats["turn_count"], 1),
        }

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
    conn.execute(
        "DELETE FROM source_sessions WHERE session_id NOT IN (SELECT session_id FROM turns)"
    )
    conn.commit()
    return removed


def get_session_ids(conn: sqlite3.Connection) -> list[str]:
    return [row["session_id"] for row in conn.execute("SELECT DISTINCT session_id FROM turns ORDER BY session_id").fetchall()]


def room_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    """先读 adapter 的显式本机 policy 结果，再从 legacy source_file 推断房间。

    一个 session 的 turns 可能散落在同一房间的多个文件（fork/撤回会拆分），但都在同一
    房间下，所以按 turn 数取多数的房间即可。全落在房间外（导出等）时返回 None。
    """
    tree_room = conn.execute(
        "SELECT room FROM conversation_card_branches WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if tree_room and tree_room["room"] in ROOMS:
        return tree_room["room"]
    routed = conn.execute(
        "SELECT room FROM source_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if routed and routed["room"] in ROOMS:
        return routed["room"]
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


def is_tree_card_session(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM conversation_card_branches WHERE session_id = ?",
        (session_id,),
    ).fetchone() is not None


def tree_session_has_uncovered_nodes(
    conn: sqlite3.Connection, session_id: str
) -> bool:
    return conn.execute(
        """
        SELECT 1
        FROM turns t
        JOIN conversation_nodes n ON n.node_id = t.source_uuid
        WHERE t.session_id = ? AND t.is_context = 0
          AND NOT EXISTS (
            SELECT 1
            FROM card_nodes covered
            JOIN cards covering_card ON covering_card.card_id = covered.card_id
            WHERE covered.node_id = n.node_id
              AND covering_card.session_id = t.session_id
          )
        LIMIT 1
        """,
        (session_id,),
    ).fetchone() is not None


def attach_card_nodes(
    conn: sqlite3.Connection, card_id: str, session_id: str,
    start_round: int | None, end_round: int | None,
) -> None:
    """Record every tree message included in a Card's inclusive turn range.

    This is Card membership, not global node ownership: rolling boundaries and
    fork context intentionally allow the same canonical node in multiple Cards.
    """
    if start_round is None:
        return
    hi = end_round if end_round is not None else start_round
    rows = conn.execute(
        """
        SELECT t.source_uuid AS node_id
        FROM turns t
        JOIN conversation_nodes n ON n.node_id = t.source_uuid
        WHERE t.session_id = ?
          AND t.round BETWEEN ? AND ?
        ORDER BY t.round, t.message_seq, t.line_no
        """,
        (session_id, start_round, hi),
    ).fetchall()
    for position, row in enumerate(rows, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO card_nodes (card_id, node_id, position) VALUES (?, ?, ?)",
            (card_id, row["node_id"], position),
        )


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


def _quote_fts(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def _jieba_query_exprs(text: str) -> tuple[list[str], list[str]]:
    """查询侧 FTS 表达式。返回 (主词表达式列表, 平铺裸词列表)。

    索引侧是 lcut 精确模式；查询词如果被 lcut 切成一整个词（如「生日礼物」），
    索引里分开写「生日 礼物」的卡就漏了。所以多字主词再用搜索模式拆子词，
    整词命中或子词全中都算：("生日礼物" OR ("生日" "礼物"))。
    平铺表是主词＋所有子词的裸词（未转义），给"任一词命中"的补位查询和
    tag 子串匹配用——调用方自己决定引号/前缀。
    纯标点的词条丢掉，免得 AND 语义下一票否决。
    """
    exprs: list[str] = []
    flat: list[str] = []
    for tok in jieba.lcut(text):
        tok = tok.strip()
        if not tok or not re.search(r"\w", tok):
            continue
        subs = [s for s in jieba.cut_for_search(tok) if s.strip() and s != tok]
        flat.append(tok)
        flat.extend(subs)
        if subs:
            sub_expr = " ".join(_quote_fts(s) for s in subs)
            exprs.append(f"({_quote_fts(tok)} OR ({sub_expr}))")
        else:
            exprs.append(_quote_fts(tok))
    return exprs, flat


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
        (card_id, session_id, turn_start, turn_end, headline, share, private, timestamp, room, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card["card_id"],
            card.get("session_id", ""),
            card.get("turns", [None, None])[0],
            card.get("turns", [None, None])[1],
            card.get("headline", ""),
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
        "INSERT INTO cards_fts (card_id, headline, share, private) VALUES (?, ?, ?, ?)",
        (
            card["card_id"],
            _jieba_seg(card.get("headline", "")),
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
