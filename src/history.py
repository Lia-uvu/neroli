"""Source-neutral read projections for conversation-tree renderers."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def _latest_observation_rows(
    conn: sqlite3.Connection,
    *,
    room: str | None = None,
    source: str | None = None,
    native_context_id: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str | int] = []
    if room:
        clauses.append("room = ?")
        params.append(room)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if native_context_id:
        clauses.append("native_context_id = ?")
        params.append(native_context_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)

    page = ""
    if limit is not None:
        page = " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT observation_id, source, room, native_context_id, kind,
               observed_at, node_id, native_node_id, payload_json
        FROM (
            SELECT observation_id, source, room, native_context_id, kind,
                   observed_at, node_id, native_node_id, payload_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY room, source, native_context_id, kind
                       ORDER BY observed_at DESC, observation_id DESC
                   ) AS recency
            FROM conversation_observations
            WHERE {' AND '.join(clauses) if clauses else '1 = 1'}
        )
        WHERE recency = 1
        ORDER BY observed_at DESC, observation_id DESC
        {page}
        """,
        params,
    ).fetchall()


def _observed_path_summary(
    conn: sqlite3.Connection, node_id: str
) -> tuple[str | None, int]:
    row = conn.execute(
        """
        WITH RECURSIVE path(node_id, parent_node_id, role, text, depth) AS (
            SELECT node_id, parent_node_id, role, text, 0
            FROM conversation_nodes
            WHERE node_id = ?
            UNION
            SELECT parent.node_id, parent.parent_node_id, parent.role,
                   parent.text, path.depth + 1
            FROM conversation_nodes parent
            JOIN path ON parent.node_id = path.parent_node_id
        )
        SELECT (
                   SELECT text
                   FROM path
                   WHERE role = 'user' AND TRIM(COALESCE(text, '')) != ''
                   ORDER BY depth DESC
                   LIMIT 1
               ) AS preview,
               COUNT(*) AS path_nodes
        FROM path
        """,
        (node_id,),
    ).fetchone()
    return row["preview"], row["path_nodes"]


def list_history_contexts(
    conn: sqlite3.Connection,
    *,
    room: str | None = None,
    source: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one bounded page of latest runtime contexts, optionally by room."""
    if not 1 <= limit <= 100:
        raise ValueError("history context limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("history context offset must be non-negative")

    rows = _latest_observation_rows(
        conn,
        room=room,
        source=source,
        kind="cursor",
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    items = []
    for row in rows[:limit]:
        preview, path_nodes = _observed_path_summary(conn, row["node_id"])
        items.append({
            "source": row["source"],
            "room": row["room"],
            "native_context_id": row["native_context_id"],
            "observed_at": row["observed_at"],
            "node_id": row["node_id"],
            "native_node_id": row["native_node_id"],
            "preview": preview,
            "path_nodes": path_nodes,
            "payload": json.loads(row["payload_json"]),
        })
    return {
        "format": "neroli-history-context-list-v1",
        "room": room,
        "source": source,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "contexts": items,
    }


def _component_nodes(
    conn: sqlite3.Connection, observation_rows: list[sqlite3.Row]
) -> list[sqlite3.Row]:
    roots: set[str] = set()
    for observation in observation_rows:
        root = conn.execute(
            """
            WITH RECURSIVE ancestors(node_id, parent_node_id) AS (
                SELECT node_id, parent_node_id
                FROM conversation_nodes
                WHERE node_id = ?
                UNION
                SELECT parent.node_id, parent.parent_node_id
                FROM conversation_nodes parent
                JOIN ancestors ON parent.node_id = ancestors.parent_node_id
            )
            SELECT node_id
            FROM ancestors
            WHERE parent_node_id IS NULL
            ORDER BY node_id
            LIMIT 1
            """,
            (observation["node_id"],),
        ).fetchone()
        if root:
            roots.add(root["node_id"])

    nodes: dict[str, sqlite3.Row] = {}
    for root_id in roots:
        for row in conn.execute(
            """
            WITH RECURSIVE component AS (
                SELECT node_id, source, room, native_node_id, parent_node_id,
                       native_parent_node_id, occurred_at, kind, source_type,
                       role, text, message_json, provider, model
                FROM conversation_nodes
                WHERE node_id = ?
                UNION
                SELECT child.node_id, child.source, child.room,
                       child.native_node_id, child.parent_node_id,
                       child.native_parent_node_id, child.occurred_at,
                       child.kind, child.source_type, child.role, child.text,
                       child.message_json, child.provider, child.model
                FROM conversation_nodes child
                JOIN component parent ON child.parent_node_id = parent.node_id
            )
            SELECT * FROM component
            ORDER BY COALESCE(occurred_at, ''), node_id
            """,
            (root_id,),
        ).fetchall():
            nodes[row["node_id"]] = row
    return list(nodes.values())


def render_history(
    conn: sqlite3.Connection,
    *,
    room: str,
    source: str | None = None,
    native_context_id: str | None = None,
) -> dict[str, Any]:
    observations = _latest_observation_rows(
        conn,
        room=room,
        source=source,
        native_context_id=native_context_id,
    )
    if native_context_id:
        nodes = _component_nodes(conn, observations)
    else:
        clauses = ["room = ?"]
        params: list[str] = [room]
        if source:
            clauses.append("source = ?")
            params.append(source)
        nodes = conn.execute(
            f"""
            SELECT node_id, source, room, native_node_id, parent_node_id,
                   native_parent_node_id, occurred_at, kind, source_type,
                   role, text, message_json, provider, model
            FROM conversation_nodes
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(occurred_at, ''), node_id
            """,
            params,
        ).fetchall()
    node_ids = {row["node_id"] for row in nodes}
    parent_by_id = {row["node_id"]: row["parent_node_id"] for row in nodes}

    rendered_nodes = []
    for row in nodes:
        message = json.loads(row["message_json"]) if row["message_json"] else None
        if message is not None:
            if row["provider"]:
                message["provider"] = row["provider"]
            if row["model"]:
                message["model"] = row["model"]
        rendered_nodes.append({
            "node_id": row["node_id"],
            "native_node_id": row["native_node_id"],
            "parent_node_id": row["parent_node_id"],
            "native_parent_node_id": row["native_parent_node_id"],
            "source": row["source"],
            "room": row["room"],
            "occurred_at": row["occurred_at"],
            "kind": row["kind"],
            "source_type": row["source_type"],
            "message": message,
        })

    rendered_observations = []
    for row in observations:
        path: list[str] = []
        current = row["node_id"]
        seen: set[str] = set()
        while current in node_ids and current not in seen:
            seen.add(current)
            path.append(current)
            current = parent_by_id.get(current)
        path.reverse()
        rendered_observations.append({
            "observation_id": row["observation_id"],
            "source": row["source"],
            "room": row["room"],
            "native_context_id": row["native_context_id"],
            "kind": row["kind"],
            "observed_at": row["observed_at"],
            "node_id": row["node_id"],
            "native_node_id": row["native_node_id"],
            "payload": json.loads(row["payload_json"]),
            "path": path,
        })
    rendered_observations.sort(
        key=lambda item: (
            item["source"], item["native_context_id"], item["kind"]
        )
    )
    roots = [row["node_id"] for row in nodes if row["parent_node_id"] is None]
    return {
        "format": "neroli-history-v1",
        "room": room,
        "source": source,
        "roots": roots,
        "nodes": rendered_nodes,
        "observations": rendered_observations,
    }
