"""Minimal read-only stdio MCP surface over Neroli retrieval.

The transport is deliberately small: JSON-RPC messages are newline-delimited on
stdin/stdout, and every memory operation delegates to ``retrieval``.  The viewer
is process-bound at startup and never appears in a tool schema, so a model cannot
change its own room or privacy scope.
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
from config import ROOM_SLUGS, ROOMS


SERVER_INFO = {"name": "neroli", "version": "1.0.0"}
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

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "neroli_search",
        "title": "Search Neroli memory",
        "description": (
            "Search visible Neroli event cards by keywords. Returns compact card "
            "references; call neroli_card to read one result. Viewer/privacy is "
            "fixed by the Porch runtime and cannot be supplied here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "since": {"type": "string", "maxLength": 64},
                "until": {"type": "string", "maxLength": 64},
            },
            "required": ["query"],
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
            "the runtime-bound viewer owns that room. Raw transcript turns are not exposed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["card_id"],
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
            "List visible cards from the same source session around one card. "
            "Returns compact references only; use neroli_card for detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
            },
            "required": ["card_id"],
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


def validate_viewer(viewer: str) -> str:
    allowed = set(ROOMS) | set(ROOM_SLUGS.values())
    if viewer not in allowed:
        raise ValueError(f"unknown Neroli viewer {viewer!r}")
    return viewer


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


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolInputError(f"unknown arguments: {', '.join(sorted(unknown))}")


def _clip(value: str, field: str, truncated: list[str]) -> str:
    if len(value) <= MAX_CARD_FIELD_CHARS:
        return value
    truncated.append(field)
    return value[:MAX_CARD_FIELD_CHARS] + "\n…[truncated by Neroli MCP]"


def call_tool(
    conn: sqlite3.Connection,
    viewer: str,
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
        if name == "neroli_search":
            _reject_unknown(arguments, {"query", "limit", "since", "until"})
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
                "cards": [
                    {
                        "card_id": item.card_id,
                        "timestamp": item.timestamp,
                        "local_time": item.local_time,
                        "room": item.room,
                        "headline": item.headline,
                        "why": item.why,
                    }
                    for item in hits
                ],
            })

        card_id = _required_text(arguments, "card_id", maximum=200)
        if name == "neroli_card":
            _reject_unknown(arguments, {"card_id"})
            card = retrieval.card_detail(conn, card_id, viewer=viewer)
            if card is None:
                return _tool_error("card not found or not visible")
            truncated: list[str] = []
            payload = asdict(card)
            payload["local_time"] = card.local_time
            for field in ("headline", "share", "private"):
                payload[field] = _clip(payload[field], field, truncated)
            payload["truncated_fields"] = truncated
            return _tool_result(payload)

        _reject_unknown(arguments, {"card_id", "limit"})
        cards = retrieval.session_siblings(conn, card_id, viewer=viewer)
        if cards is None:
            return _tool_error("card not found or not visible")
        limit = _limit(arguments, default=12, maximum=30)
        selected_index = next(
            (index for index, item in enumerate(cards) if item.card_id == card_id), 0
        )
        start = max(0, selected_index - limit // 2)
        end = min(len(cards), start + limit)
        start = max(0, end - limit)
        window = cards[start:end]
        return _tool_result({
            "card_id": card_id,
            "total_cards": len(cards),
            "window_start": start,
            "selected_index": selected_index,
            "cards": [
                {
                    "card_id": item.card_id,
                    "timestamp": item.timestamp,
                    "local_time": item.local_time,
                    "room": item.room,
                    "headline": item.headline,
                    "turn_start": item.turn_start,
                    "turn_end": item.turn_end,
                    "selected": item.card_id == card_id,
                }
                for item in window
            ],
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


def dispatch(conn: sqlite3.Connection, viewer: str, request: Any) -> dict[str, Any] | None:
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
                "and use session context when chronology matters."
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
            result = call_tool(conn, viewer, params["name"], params.get("arguments"))
        except Exception as exc:  # Protocol boundary: return a tool failure, never corrupt stdout.
            print(f"[neroli-mcp] tool failure: {exc}", file=sys.stderr)
            result = _tool_error("Neroli retrieval failed")
        return _response(request_id, result)
    return _error(request_id, -32601, f"Method not found: {method}")


def serve(
    conn: sqlite3.Connection,
    viewer: str,
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
            response = dispatch(conn, viewer, request)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Neroli read-only stdio MCP server")
    parser.add_argument("--db", type=Path, default=db.DB)
    parser.add_argument("--viewer", required=True)
    args = parser.parse_args(argv)
    viewer = validate_viewer(args.viewer)
    conn = connect_readonly(args.db)
    try:
        serve(conn, viewer)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
