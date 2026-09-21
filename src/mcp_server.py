"""Minimal read-only stdio MCP surface over Neroli retrieval.

The transport is deliberately small: JSON-RPC messages are newline-delimited on
stdin/stdout, and every memory operation delegates to ``retrieval``.  Each call
declares its viewer; runtimes may provide a process-level default for compatibility.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

import db
import retrieval
SERVER_INFO = {"name": "neroli", "version": "1.1.0"}
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
}
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
MAX_QUERY_CHARS = 500
MAX_CARD_FIELD_CHARS = 12_000
MAX_EXPANDED_CARDS = 5
MAX_TURNS_CHARS = 48_000

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "neroli_search",
        "title": "Search Neroli memory",
        "description": (
            "Search visible Neroli event cards by keywords. Returns compact card "
            "references; call neroli_card to read one result. Pass the viewer "
            "whose room visibility should apply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewer": {"type": "string", "minLength": 1, "maxLength": 200},
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "since": {"type": "string", "maxLength": 64},
                "until": {"type": "string", "maxLength": 64},
                "expand": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_EXPANDED_CARDS,
                    "default": 3,
                    "description": "Expand the first N results as full visible cards.",
                },
            },
            "required": ["viewer", "query"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "neroli_card",
        "title": "Read a Neroli card",
        "description": (
            "Read one visible Neroli event card, including private text only when "
            "the supplied viewer owns that room. Raw transcript turns are optional "
            "and require a same-room viewer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewer": {"type": "string", "minLength": 1, "maxLength": 200},
                "card_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "include_turns": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include raw transcript turns for a same-room viewer.",
                },
            },
            "required": ["viewer", "card_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "neroli_session_context",
        "title": "Read surrounding cards",
        "description": (
            "Read visible cards from the same source session around or before one "
            "card, in narrative order, with bounded batch expansion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewer": {"type": "string", "minLength": 1, "maxLength": 200},
                "card_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
                "direction": {
                    "type": "string",
                    "enum": ["around", "before"],
                    "default": "around",
                },
                "expand": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_EXPANDED_CARDS,
                    "default": 3,
                    "description": "Expand up to N cards in narrative order.",
                },
            },
            "required": ["viewer", "card_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "neroli_wander",
        "title": "Wander through older Neroli cards",
        "description": (
            "Surface older visible cards, preferring cards opened least often and "
            "randomizing among equally cold cards. Expands the first three by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewer": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
                "expand": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_EXPANDED_CARDS,
                    "default": 3,
                },
            },
            "required": ["viewer"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "neroli_recent",
        "title": "Browse Neroli cards by time",
        "description": (
            "List visible cards in a time range, newest first. Expands the first "
            "three by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewer": {"type": "string", "minLength": 1, "maxLength": 200},
                "since": {"type": "string", "maxLength": 64},
                "until": {"type": "string", "maxLength": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40},
                "expand": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_EXPANDED_CARDS,
                    "default": 3,
                },
            },
            "required": ["viewer"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)
TOOL_NAMES = {tool["name"] for tool in TOOLS}


class ToolInputError(ValueError):
    pass


def connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Neroli database does not exist: {resolved}")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != db.SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"database schema version {version} != expected {db.SCHEMA_VERSION}"
        )
    return conn


def _required_text(arguments: dict[str, Any], key: str, *, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise ToolInputError(f"{key} exceeds {maximum} characters")
    return value


def _optional_text(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ToolInputError(f"{key} must be a string of at most 64 characters")
    return value or None


def _limit(arguments: dict[str, Any], *, default: int, maximum: int) -> int:
    value = arguments.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ToolInputError(f"limit must be an integer from 1 to {maximum}")
    return value


def _expand_count(arguments: dict[str, Any], *, default: int = 3) -> int:
    value = arguments.get("expand", default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_EXPANDED_CARDS
    ):
        raise ToolInputError(
            f"expand must be an integer from 0 to {MAX_EXPANDED_CARDS}"
        )
    return value


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolInputError(f"unknown arguments: {', '.join(sorted(unknown))}")


def _clip(value: str, field: str, truncated: list[str]) -> str:
    if len(value) <= MAX_CARD_FIELD_CHARS:
        return value
    truncated.append(field)
    return value[:MAX_CARD_FIELD_CHARS] + "\n…[truncated by Neroli MCP]"


def _card_ref_payload(item: retrieval.CardRef | retrieval.CardDetail) -> dict[str, Any]:
    payload = {
        "card_id": item.card_id,
        "timestamp": item.timestamp,
        "local_time": item.local_time,
        "room": item.room,
        "headline": item.headline,
    }
    if isinstance(item, retrieval.CardRef):
        payload["why"] = item.why
    else:
        payload["turn_start"] = item.turn_start
        payload["turn_end"] = item.turn_end
    return payload


def _card_detail_payload(card: retrieval.CardDetail) -> dict[str, Any]:
    truncated: list[str] = []
    payload = asdict(card)
    payload["local_time"] = card.local_time
    for field in ("headline", "share", "private"):
        payload[field] = _clip(payload[field], field, truncated)
    payload["truncated_fields"] = truncated
    return payload


def _expanded_payloads(
    conn: sqlite3.Connection,
    refs: list[retrieval.CardRef | retrieval.CardDetail],
    viewer: str,
    count: int,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for ref in refs[:count]:
        detail = (
            ref
            if isinstance(ref, retrieval.CardDetail)
            else retrieval.card_detail(conn, ref.card_id, viewer=viewer)
        )
        if detail is not None:
            expanded.append(_card_detail_payload(detail))
    return expanded


def _turns_payload(turns: list[retrieval.TurnRow]) -> tuple[list[dict[str, Any]], bool]:
    payloads: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for turn in turns:
        remaining = MAX_TURNS_CHARS - used
        if remaining <= 0:
            truncated = True
            break
        text = turn.text
        if len(text) > min(MAX_CARD_FIELD_CHARS, remaining):
            text = text[: min(MAX_CARD_FIELD_CHARS, remaining)] + "\n…[truncated by Neroli MCP]"
            truncated = True
        used += len(text)
        payloads.append({
            "round": turn.round,
            "speaker": turn.speaker,
            "timestamp": turn.timestamp,
            "local_time": turn.local_time,
            "text": text,
        })
    return payloads, truncated


def call_tool(
    conn: sqlite3.Connection,
    default_viewer: str | None,
    name: str,
    raw_arguments: Any,
) -> dict[str, Any]:
    if name not in TOOL_NAMES:
        return _tool_error(f"unknown Neroli tool: {name}")
    if raw_arguments is None:
        arguments: dict[str, Any] = {}
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        return _tool_error("tool arguments must be an object")
    try:
        viewer = arguments.get("viewer", default_viewer)
        if not isinstance(viewer, str) or not viewer.strip():
            raise ToolInputError("viewer must be a non-empty string")
        viewer = viewer.strip()
        if len(viewer) > 200:
            raise ToolInputError("viewer exceeds 200 characters")
        if name == "neroli_search":
            _reject_unknown(
                arguments, {"viewer", "query", "limit", "since", "until", "expand"}
            )
            query = _required_text(arguments, "query", maximum=MAX_QUERY_CHARS)
            hits = retrieval.search(
                conn,
                query,
                limit=_limit(arguments, default=8, maximum=20),
                viewer=viewer,
                since=_optional_text(arguments, "since"),
                until=_optional_text(arguments, "until"),
            )
            return _tool_result({
                "query": query,
                "count": len(hits),
                "cards": [_card_ref_payload(item) for item in hits],
                "expanded_cards": _expanded_payloads(
                    conn, hits, viewer, _expand_count(arguments)
                ),
            })

        if name == "neroli_wander":
            _reject_unknown(arguments, {"viewer", "limit", "expand"})
            hits = retrieval.wander(
                conn,
                limit=_limit(arguments, default=3, maximum=20),
                viewer=viewer,
            )
            return _tool_result({
                "count": len(hits),
                "cards": [_card_ref_payload(item) for item in hits],
                "expanded_cards": _expanded_payloads(
                    conn, hits, viewer, _expand_count(arguments)
                ),
            })

        if name == "neroli_recent":
            _reject_unknown(arguments, {"viewer", "since", "until", "limit", "expand"})
            hits = retrieval.recent(
                conn,
                since=_optional_text(arguments, "since"),
                until=_optional_text(arguments, "until"),
                limit=_limit(arguments, default=40, maximum=100),
                viewer=viewer,
            )
            return _tool_result({
                "count": len(hits),
                "cards": [_card_ref_payload(item) for item in hits],
                "expanded_cards": _expanded_payloads(
                    conn, hits, viewer, _expand_count(arguments)
                ),
            })

        card_id = _required_text(arguments, "card_id", maximum=200)
        if name == "neroli_card":
            _reject_unknown(arguments, {"viewer", "card_id", "include_turns"})
            include_turns = arguments.get("include_turns", False)
            if not isinstance(include_turns, bool):
                raise ToolInputError("include_turns must be a boolean")
            card = retrieval.card_detail(conn, card_id, viewer=viewer)
            if card is None:
                return _tool_error("card not found or not visible")
            payload = _card_detail_payload(card)
            if include_turns:
                turns = retrieval.card_turns(conn, card_id, viewer=viewer)
                if turns is None:
                    payload["turns"] = None
                    payload["turns_unavailable"] = "raw turns require a same-room viewer"
                else:
                    payload["turns"], payload["turns_truncated"] = _turns_payload(turns)
            return _tool_result(payload)

        _reject_unknown(arguments, {"viewer", "card_id", "limit", "direction", "expand"})
        direction = arguments.get("direction", "around")
        if direction not in {"around", "before"}:
            raise ToolInputError("direction must be 'around' or 'before'")
        limit = _limit(arguments, default=12, maximum=30)
        cards = (
            retrieval.session_siblings(conn, card_id, viewer=viewer)
            if direction == "around"
            else retrieval.session_before(conn, card_id, limit=limit, viewer=viewer)
        )
        if cards is None:
            return _tool_error("card not found or not visible")
        if direction == "around":
            selected_index = next(
                (index for index, item in enumerate(cards) if item.card_id == card_id), 0
            )
            start = max(0, selected_index - limit // 2)
            end = min(len(cards), start + limit)
            start = max(0, end - limit)
            window = cards[start:end]
            window_selected_index = selected_index - start
            expand_count = _expand_count(arguments)
            expand_start = max(0, window_selected_index - expand_count // 2)
            expand_end = min(len(window), expand_start + expand_count)
            expand_start = max(0, expand_end - expand_count)
            expand_cards = window[expand_start:expand_end]
        else:
            selected_index = None
            start = 0
            window = cards
            expand_count = _expand_count(arguments)
            expand_cards = window[-expand_count:] if expand_count else []
        return _tool_result({
            "card_id": card_id,
            "direction": direction,
            "total_cards": len(cards),
            "window_start": start,
            "selected_index": selected_index,
            "cards": [
                {**_card_ref_payload(item), "selected": item.card_id == card_id}
                for item in window
            ],
            "expanded_cards": _expanded_payloads(
                conn, list(expand_cards), viewer, len(expand_cards)
            ),
        })
    except ToolInputError as exc:
        return _tool_error(str(exc))


def _tool_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        ]
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def dispatch(conn: sqlite3.Connection, default_viewer: str | None, request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _error(request.get("id") if isinstance(request, dict) else None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if "id" not in request:
        return None
    if method == "initialize":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return _response(request_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Neroli tools are read-only. Search first, expand only relevant cards, "
                "use session context when chronology matters, and pass the viewer "
                "whose room visibility should apply to every call."
            ),
        })
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": list(TOOLS)})
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "Invalid tools/call parameters")
        try:
            result = call_tool(conn, default_viewer, params["name"], params.get("arguments"))
        except Exception as exc:  # Protocol boundary: return a tool failure, never corrupt stdout.
            print(f"[neroli-mcp] tool failure: {exc}", file=sys.stderr)
            result = _tool_error("Neroli retrieval failed")
        return _response(request_id, result)
    return _error(request_id, -32601, f"Method not found: {method}")


def serve(
    conn: sqlite3.Connection,
    default_viewer: str | None,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        else:
            response = dispatch(conn, default_viewer, request)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Neroli read-only stdio MCP server")
    parser.add_argument("--db", type=Path, default=db.DB)
    parser.add_argument(
        "--viewer",
        help="optional default viewer for callers that omit the tool argument",
    )
    args = parser.parse_args(argv)
    conn = connect_readonly(args.db)
    try:
        serve(conn, args.viewer)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
