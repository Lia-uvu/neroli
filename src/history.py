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
                       PARTITION BY room, source, native_context_id, kind,
                                    CASE WHEN kind = 'archive-root' THEN node_id ELSE '' END
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


def _latest_catalog_rows(
    conn: sqlite3.Connection,
    *,
    room: str | None,
    source: str | None,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    """Return one representative row per cursor/archive context.

    ``archive-root`` may have several current rows for one context because an
    exported conversation can contain disconnected captured components.  The
    catalog still presents that source context once; selected rendering loads all
    of its roots through ``_latest_observation_rows``.
    """
    clauses = ["kind IN ('cursor', 'archive-root')"]
    params: list[str | int] = []
    if room:
        clauses.append("room = ?")
        params.append(room)
    if source:
        clauses.append("source = ?")
        params.append(source)
    params.extend([limit, offset])
    return conn.execute(
        f"""
        WITH latest_per_key AS (
            SELECT observation_id, source, room, native_context_id, kind,
                   observed_at, node_id, native_node_id, payload_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY room, source, native_context_id, kind,
                                    CASE WHEN kind = 'archive-root' THEN node_id ELSE '' END
                       ORDER BY observed_at DESC, observation_id DESC
                   ) AS key_recency
            FROM conversation_observations
            WHERE {' AND '.join(clauses)}
        ), catalog AS (
            SELECT observation_id, source, room, native_context_id, kind,
                   observed_at, node_id, native_node_id, payload_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY room, source, native_context_id
                       ORDER BY observed_at DESC,
                                CASE WHEN kind = 'cursor' THEN 0 ELSE 1 END,
                                observation_id DESC
                   ) AS context_recency
            FROM latest_per_key
            WHERE key_recency = 1
        )
        SELECT observation_id, source, room, native_context_id, kind,
               observed_at, node_id, native_node_id, payload_json
        FROM catalog
        WHERE context_recency = 1
        ORDER BY observed_at DESC, observation_id DESC
        LIMIT ? OFFSET ?
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

    rows = _latest_catalog_rows(
        conn,
        room=room,
        source=source,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    items = []
    for row in rows[:limit]:
        if row["kind"] == "archive-root":
            context_observations = _latest_observation_rows(
                conn,
                room=row["room"],
                source=row["source"],
                native_context_id=row["native_context_id"],
                kind="archive-root",
            )
            component = _component_nodes(conn, context_observations)
            user_rows = [
                node for node in component
                if node["role"] == "user" and (node["text"] or "").strip()
            ]
            user_rows.sort(key=lambda node: (node["occurred_at"] or "", node["node_id"]))
            preview = user_rows[0]["text"] if user_rows else None
            path_nodes = len(component)
        else:
            preview, path_nodes = _observed_path_summary(conn, row["node_id"])
        items.append({
            "source": row["source"],
            "room": row["room"],
            "native_context_id": row["native_context_id"],
            "kind": row["kind"],
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


def search_history_contexts(
    conn: sqlite3.Connection,
    *,
    query: str,
    room: str | None = None,
    source: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search canonical portable text and return bounded conversation contexts.

    This intentionally scans ``conversation_nodes`` instead of introducing a
    persisted search index. Search is a human-local History projection: the
    canonical tree remains the only source of truth, and MCP is not involved.
    """
    if not 1 <= limit <= 100:
        raise ValueError("history search limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("history search offset must be non-negative")
    terms = _history_search_terms(query)

    clauses = [
        "role IN ('user', 'assistant')",
        "TRIM(COALESCE(text, '')) != ''",
    ]
    params: list[str] = []
    if room:
        clauses.append("room = ?")
        params.append(room)
    if source:
        clauses.append("source = ?")
        params.append(source)
    term_clauses = []
    for term in terms:
        term_clauses.append("INSTR(LOWER(text), LOWER(?)) > 0")
        params.append(term)
    clauses.append(f"({' OR '.join(term_clauses)})")
    matching_nodes = conn.execute(
        f"""
        SELECT node_id, parent_node_id, text, occurred_at
        FROM conversation_nodes
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(occurred_at, '') DESC, node_id DESC
        """,
        params,
    ).fetchall()

    parent_clauses: list[str] = []
    parent_params: list[str] = []
    if room:
        parent_clauses.append("room = ?")
        parent_params.append(room)
    if source:
        parent_clauses.append("source = ?")
        parent_params.append(source)
    parent_rows = conn.execute(
        f"""
        SELECT node_id, parent_node_id
        FROM conversation_nodes
        WHERE {' AND '.join(parent_clauses) if parent_clauses else '1 = 1'}
        """,
        parent_params,
    ).fetchall()
    parent_by_id = {row["node_id"]: row["parent_node_id"] for row in parent_rows}
    root_cache: dict[str, str] = {}

    observations = [
        row for row in _latest_observation_rows(conn, room=room, source=source)
        if row["kind"] in {"cursor", "archive-root"}
    ]
    contexts_by_root: dict[str, set[tuple[str, str, str]]] = {}
    representative: dict[tuple[str, str, str], sqlite3.Row] = {}
    for observation in observations:
        key = (
            observation["room"],
            observation["source"],
            observation["native_context_id"],
        )
        root = _history_root(observation["node_id"], parent_by_id, root_cache)
        contexts_by_root.setdefault(root, set()).add(key)
        current = representative.get(key)
        candidate_order = (
            observation["observed_at"],
            observation["kind"] == "cursor",
            observation["observation_id"],
        )
        current_order = (
            current["observed_at"],
            current["kind"] == "cursor",
            current["observation_id"],
        ) if current is not None else None
        if current_order is None or candidate_order > current_order:
            representative[key] = observation

    matched_terms: dict[tuple[str, str, str], set[str]] = {}
    snippets: dict[tuple[str, str, str], tuple[int, str]] = {}
    for node in matching_nodes:
        text = node["text"] or ""
        folded = text.casefold()
        present = {term for term in terms if term.casefold() in folded}
        if not present:
            continue
        root = _history_root(node["node_id"], parent_by_id, root_cache)
        for key in contexts_by_root.get(root, set()):
            matched_terms.setdefault(key, set()).update(present)
            snippet = _history_search_snippet(text, terms)
            current = snippets.get(key)
            score = len(present)
            if current is None or score > current[0]:
                snippets[key] = (score, snippet)

    matches = [
        (key, representative[key])
        for key, found in matched_terms.items()
        if all(term in found for term in terms)
    ]
    matches.sort(
        key=lambda item: (
            item[1]["observed_at"],
            item[1]["observation_id"],
        ),
        reverse=True,
    )
    selected = matches[offset:offset + limit]
    items = []
    for key, row in selected:
        if row["kind"] == "archive-root":
            context_observations = _latest_observation_rows(
                conn,
                room=row["room"],
                source=row["source"],
                native_context_id=row["native_context_id"],
                kind="archive-root",
            )
            component = _component_nodes(conn, context_observations)
            path_nodes = len(component)
            user_rows = [
                node for node in component
                if node["role"] == "user" and (node["text"] or "").strip()
            ]
            user_rows.sort(key=lambda node: (node["occurred_at"] or "", node["node_id"]))
            preview = user_rows[0]["text"] if user_rows else None
        else:
            preview, path_nodes = _observed_path_summary(conn, row["node_id"])
        items.append({
            "source": row["source"],
            "room": row["room"],
            "native_context_id": row["native_context_id"],
            "kind": row["kind"],
            "observed_at": row["observed_at"],
            "node_id": row["node_id"],
            "native_node_id": row["native_node_id"],
            "preview": preview,
            "snippet": snippets[key][1],
            "path_nodes": path_nodes,
            "payload": json.loads(row["payload_json"]),
        })
    return {
        "format": "neroli-history-search-v1",
        "query": query.strip(),
        "room": room,
        "source": source,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(matches),
        "contexts": items,
    }


def _history_search_terms(query: str) -> list[str]:
    normalized = query.strip()
    if not normalized:
        raise ValueError("history search query must not be empty")
    if len(normalized) > 200:
        raise ValueError("history search query must not exceed 200 characters")
    terms = list(dict.fromkeys(part.casefold() for part in normalized.split() if part))
    if len(terms) > 12:
        raise ValueError("history search query must not exceed 12 terms")
    return terms


def _history_root(
    node_id: str,
    parent_by_id: dict[str, str | None],
    cache: dict[str, str],
) -> str:
    if node_id in cache:
        return cache[node_id]
    path: list[str] = []
    current = node_id
    seen: set[str] = set()
    while current in parent_by_id and parent_by_id[current] is not None:
        if current in seen:
            raise ValueError(f"conversation tree cycle while searching at {current!r}")
        seen.add(current)
        path.append(current)
        current = parent_by_id[current]  # type: ignore[assignment]
        if current in cache:
            current = cache[current]
            break
    for item in path:
        cache[item] = current
    cache[node_id] = current
    return current


def _history_search_snippet(text: str, terms: list[str], width: int = 140) -> str:
    normalized = " ".join(text.split())
    folded = normalized.casefold()
    positions = [folded.find(term.casefold()) for term in terms]
    hits = [position for position in positions if position >= 0]
    center = min(hits) if hits else 0
    start = max(0, center - width // 3)
    end = min(len(normalized), start + width)
    snippet = normalized[start:end]
    if start > 0:
        snippet = f"…{snippet}"
    if end < len(normalized):
        snippet = f"{snippet}…"
    return snippet


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
        if row["kind"] == "cursor":
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
