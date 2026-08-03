"""Source-neutral read projection for conversation-tree renderers."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def render_history(
    conn: sqlite3.Connection,
    *,
    room: str,
    source: str | None = None,
    native_context_id: str | None = None,
) -> dict[str, Any]:
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

    observation_clauses = ["room = ?"]
    observation_params: list[str] = [room]
    if source:
        observation_clauses.append("source = ?")
        observation_params.append(source)
    if native_context_id:
        observation_clauses.append("native_context_id = ?")
        observation_params.append(native_context_id)
    observations = conn.execute(
        f"""
        SELECT observation_id, source, room, native_context_id, kind,
               observed_at, node_id, native_node_id, payload_json
        FROM conversation_observations
        WHERE {' AND '.join(observation_clauses)}
        ORDER BY observed_at, observation_id
        """,
        observation_params,
    ).fetchall()

    latest: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in observations:
        latest[(row["source"], row["native_context_id"], row["kind"])] = row

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
    for row in latest.values():
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
