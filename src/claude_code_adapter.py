"""Claude Code JSONL -> source-neutral conversation-tree adapter.

The native journal is richer than Neroli's portable tree contract.  This module
keeps stable UUID/parent structure and portable user/assistant text while leaving
tool payloads, prompts, credentials, and other runtime-private fields in the
original JSONL files.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from loaders import clean_user_text


SOURCE = "claude-code"


@dataclass(frozen=True)
class _Occurrence:
    native_node_id: str
    native_parent_node_id: str | None
    occurred_at: str | None
    kind: str
    source_type: str
    message: dict[str, Any] | None
    is_compaction: bool
    file: Path
    line_no: int

    def immutable_facts(self) -> tuple[Any, ...]:
        return (
            self.occurred_at,
            self.kind,
            self.source_type,
            json.dumps(
                self.message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) if self.message is not None else None,
        )


def export_claude_code_tree(
    paths: Iterable[Path],
    *,
    room: str,
    source: str = SOURCE,
) -> dict[str, Any]:
    """Return one deterministic tree-v1 envelope for an enrolled room.

    Replayed UUIDs are collapsed after checking immutable portable facts.  A
    replay may temporarily re-parent an old message below a compaction summary;
    when exactly one non-compaction parent exists, that original parent wins.
    Any genuinely ambiguous non-compaction parent reuse fails loudly.
    """
    ordered_paths = sorted({Path(path) for path in paths}, key=str)
    occurrences: dict[str, list[_Occurrence]] = {}
    compaction_ids: set[str] = set()
    cursor_candidates: dict[str, list[tuple[float, str, int, str]]] = {}
    parent_overrides: dict[str, str] = {}

    for path in ordered_paths:
        try:
            observed_mtime = path.stat().st_mtime
        except OSError:
            continue
        source_rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle):
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid Claude Code JSONL at {path}:{line_no + 1}"
                    ) from exc
                if not isinstance(raw, dict):
                    continue
                source_rows.append(raw)
                if raw.get("type") == "last-prompt":
                    context_id = _string(raw.get("sessionId"))
                    leaf_id = _string(raw.get("leafUuid"))
                    if context_id and leaf_id:
                        cursor_candidates.setdefault(context_id, []).append(
                            (observed_mtime, str(path), line_no, leaf_id)
                        )

                native_node_id = _string(raw.get("uuid"))
                if not native_node_id:
                    continue
                occurrence = _normalize_occurrence(raw, path, line_no)
                occurrences.setdefault(native_node_id, []).append(occurrence)
                if occurrence.is_compaction:
                    compaction_ids.add(native_node_id)
        for node_id, parent_id in _source_parent_overrides(source_rows, path).items():
            previous = parent_overrides.get(node_id)
            if previous is not None and previous != parent_id:
                raise ValueError(
                    f"Claude Code UUID {node_id!r} has conflicting normalized parents"
                )
            parent_overrides[node_id] = parent_id

    nodes: list[dict[str, Any]] = []
    selected_ids = set(occurrences)
    for native_node_id, group in occurrences.items():
        baseline = group[0].immutable_facts()
        if any(item.immutable_facts() != baseline for item in group[1:]):
            raise ValueError(
                f"Claude Code UUID {native_node_id!r} has conflicting portable content"
            )
        if native_node_id in parent_overrides:
            parent_id = parent_overrides[native_node_id]
        else:
            parent_id = _resolve_parent(native_node_id, group, compaction_ids)
        if parent_id is not None and parent_id not in selected_ids:
            raise ValueError(
                f"Claude Code UUID {native_node_id!r} has missing parent {parent_id!r}"
            )
        chosen = min(group, key=_occurrence_order)
        node: dict[str, Any] = {
            "native_node_id": native_node_id,
            "native_parent_node_id": parent_id,
            "occurred_at": chosen.occurred_at,
            "kind": chosen.kind,
            "source_type": chosen.source_type,
        }
        if chosen.message is not None:
            node["message"] = chosen.message
        nodes.append(node)

    nodes.sort(key=lambda item: (item.get("occurred_at") or "", item["native_node_id"]))
    observations: list[dict[str, Any]] = []
    for context_id, candidates in sorted(cursor_candidates.items()):
        observed_mtime, _path, _line_no, leaf_id = max(candidates)
        if leaf_id not in selected_ids:
            continue
        observations.append(
            {
                "native_context_id": context_id,
                "kind": "cursor",
                "native_node_id": leaf_id,
                "observed_at": _timestamp_from_epoch(observed_mtime),
                "payload": {"runtime": "claude-code"},
            }
        )

    return {
        "format": "neroli-conversation-tree-v1",
        "source": source,
        "room": room,
        "nodes": nodes,
        "observations": observations,
    }


def _normalize_occurrence(raw: dict[str, Any], path: Path, line_no: int) -> _Occurrence:
    native_node_id = _string(raw.get("uuid"))
    assert native_node_id is not None
    parent_id = _string(raw.get("parentUuid"))
    occurred_at = _optional_timestamp(raw.get("timestamp"), path, line_no)
    raw_type = _string(raw.get("type")) or "event"
    is_compaction = bool(raw.get("isCompactSummary"))
    source_type = f"{raw_type}:compact-summary" if is_compaction else raw_type
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    role = message.get("role")
    portable_content = _portable_content(message.get("content"), role)

    normalized_message: dict[str, Any] | None = None
    if is_compaction:
        kind = "checkpoint"
    elif _is_nonportable_event(raw, raw_type):
        kind = "event"
    elif _has_tool_content(raw, message):
        kind = "tool"
    elif role in {"user", "assistant"} and portable_content:
        kind = "message"
        normalized_message = {
            "role": role,
            "content": portable_content,
        }
        provider = _string(raw.get("provider")) or _string(message.get("provider"))
        model = _string(raw.get("model")) or _string(message.get("model"))
        if role == "assistant" and provider:
            normalized_message["provider"] = provider
        if role == "assistant" and model:
            normalized_message["model"] = model
    else:
        kind = "event"

    return _Occurrence(
        native_node_id=native_node_id,
        native_parent_node_id=parent_id,
        occurred_at=occurred_at,
        kind=kind,
        source_type=source_type,
        message=normalized_message,
        is_compaction=is_compaction,
        file=path,
        line_no=line_no,
    )


def _portable_content(content: Any, role: Any) -> list[dict[str, str]]:
    """Keep native text blocks byte-for-byte unless filtering runtime injection.

    Claude sometimes embeds system reminders and command echoes inside a user
    text block.  Those continue through the legacy cleaner.  Ordinary captured
    text keeps its original block boundaries and whitespace for canonical
    history; the derived flat ``conversation_nodes.text`` remains a convenience
    projection only.
    """
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = [
            {"type": "text", "text": item["text"]}
            for item in content
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
        ]
    else:
        return []
    if not any(block["text"].strip() for block in blocks):
        return []
    if role != "user":
        return blocks

    legacy_text = "\n".join(block["text"] for block in blocks).strip()
    cleaned = clean_user_text(legacy_text)
    if not cleaned:
        return []
    if cleaned == legacy_text:
        return blocks
    return [{"type": "text", "text": cleaned}]


def _resolve_parent(
    native_node_id: str,
    group: list[_Occurrence],
    compaction_ids: set[str],
) -> str | None:
    parents = {item.native_parent_node_id for item in group}
    if len(parents) == 1:
        return next(iter(parents))
    non_compaction = {parent for parent in parents if parent not in compaction_ids}
    if len(non_compaction) == 1:
        return next(iter(non_compaction))
    raise ValueError(
        f"Claude Code UUID {native_node_id!r} has ambiguous parent variants"
    )


def _source_parent_overrides(
    rows: list[dict[str, Any]], path: Path
) -> dict[str, str]:
    """Normalize Claude runtime bookkeeping into conversational parent edges.

    Claude records a successful retry as a sibling of its preceding ``api_error``
    and leaves the stop hook attached to the error.  That is an API-attempt
    journal, not a conversational fork.  A compact boundary likewise carries its
    actual continuation edge in ``logicalParentUuid`` while ``parentUuid`` is
    empty.  Both facts are explicit source metadata, so using them does not infer
    structure from content or JSONL append order.
    """
    overrides: dict[str, str] = {}

    def set_override(node_id: str, parent_id: str) -> None:
        previous = overrides.get(node_id)
        if previous is not None and previous != parent_id:
            raise ValueError(
                f"conflicting Claude lifecycle parents for {node_id!r} in {path}"
            )
        overrides[node_id] = parent_id

    for raw in rows:
        if raw.get("type") != "system" or raw.get("subtype") != "compact_boundary":
            continue
        node_id = _string(raw.get("uuid"))
        logical_parent_id = _string(raw.get("logicalParentUuid"))
        if node_id and logical_parent_id:
            set_override(node_id, logical_parent_id)

    siblings: dict[tuple[str, str], list[dict[str, Any]]] = {}
    children: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in rows:
        session_id = _string(raw.get("sessionId"))
        parent_id = _string(raw.get("parentUuid"))
        node_id = _string(raw.get("uuid"))
        if not session_id or not parent_id or not node_id:
            continue
        siblings.setdefault((session_id, parent_id), []).append(raw)
        children.setdefault((session_id, parent_id), []).append(raw)

    for (session_id, parent_id), group in siblings.items():
        errors = [
            raw for raw in group
            if raw.get("type") == "system" and raw.get("subtype") == "api_error"
        ]
        assistants = [raw for raw in group if raw.get("type") == "assistant"]
        if not errors or len(assistants) != 1:
            continue
        assistant = assistants[0]
        assistant_id = _string(assistant.get("uuid"))
        assistant_time = _optional_timestamp(assistant.get("timestamp"), path, -1)
        ordered_errors = sorted(
            errors,
            key=lambda raw: (
                _optional_timestamp(raw.get("timestamp"), path, -1) or "",
                _string(raw.get("uuid")) or "",
            ),
        )
        error_times = [
            _optional_timestamp(raw.get("timestamp"), path, -1)
            for raw in ordered_errors
        ]
        if (
            not assistant_id
            or not assistant_time
            or any(not value or value >= assistant_time for value in error_times)
        ):
            continue

        previous_id = parent_id
        error_ids: list[str] = []
        for error in ordered_errors:
            error_id = _string(error.get("uuid"))
            assert error_id is not None
            error_ids.append(error_id)
            if previous_id != parent_id:
                set_override(error_id, previous_id)
            previous_id = error_id
        set_override(assistant_id, previous_id)

        hooks = [
            raw
            for error_id in error_ids
            for raw in children.get((session_id, error_id), [])
            if (
                raw.get("type") == "system"
                and raw.get("subtype") == "stop_hook_summary"
                and (
                    _optional_timestamp(raw.get("timestamp"), path, -1) or ""
                ) >= assistant_time
            )
        ]
        if len(hooks) > 1:
            raise ValueError(
                f"ambiguous Claude retry stop hooks below {parent_id!r} in {path}"
            )
        if hooks:
            hook_id = _string(hooks[0].get("uuid"))
            assert hook_id is not None
            set_override(hook_id, assistant_id)

    return overrides


def _has_tool_content(raw: dict[str, Any], message: dict[str, Any]) -> bool:
    if "toolUseResult" in raw:
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") in {"tool_use", "tool_result"}
        for item in content
    )


def _is_nonportable_event(raw: dict[str, Any], raw_type: str) -> bool:
    """Keep Claude runtime/meta/error rows structural even when they carry text."""
    if raw_type not in {"user", "assistant"}:
        return True
    return bool(
        raw.get("isMeta")
        or raw.get("isVisibleInTranscriptOnly")
        or raw.get("isApiErrorMessage")
        or raw.get("error")
        or raw.get("apiErrorStatus")
    )


def _occurrence_order(item: _Occurrence) -> tuple[str, str, int]:
    return (item.occurred_at or "", str(item.file), item.line_no)


def _optional_timestamp(value: Any, path: Path, line_no: int) -> str | None:
    text = _string(value)
    if text is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid Claude Code timestamp at {path}:{line_no + 1}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError(
            f"Claude Code timestamp must be UTC at {path}:{line_no + 1}"
        )
    return parsed.astimezone(dt.UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _timestamp_from_epoch(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
