"""Claude.ai archive export -> source-neutral conversation-tree adapter.

The export contains stable conversation/message UUIDs and explicit parent UUIDs.
Conversation membership is preserved as ``archive-root`` observations: one per
captured connected component.  These observations make an archive conversation
addressable by History without pretending that the export exposes an active UI
cursor.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SOURCE = "claude-ai"
ARCHIVE_OBSERVATION_KIND = "archive-root"


@dataclass(frozen=True)
class ClaudeAiTreeExport:
    envelope: dict[str, Any]
    report: dict[str, int | str | None]


@dataclass(frozen=True)
class _NodeOccurrence:
    native_node_id: str
    raw_parent_id: str | None
    occurred_at: str | None
    kind: str
    source_type: str
    message: dict[str, Any] | None

    def immutable_facts(self) -> tuple[Any, ...]:
        return (
            self.raw_parent_id,
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


@dataclass(frozen=True)
class _ContextSnapshot:
    native_context_id: str
    observed_at: str | None
    native_node_ids: frozenset[str]
    occurrences: tuple[_NodeOccurrence, ...]
    path: Path
    index: int


def build_claude_ai_tree(
    paths: Iterable[Path],
    *,
    room: str,
    source: str = SOURCE,
) -> ClaudeAiTreeExport:
    """Build one deterministic tree-v1 envelope from full archive snapshots.

    Duplicate exports are collapsed by native UUID after immutable portable facts
    are checked.  A referenced parent absent from every selected export becomes a
    detached root; the adapter never guesses an edge from timestamps or array order.
    """
    ordered_paths = sorted({Path(path) for path in paths}, key=str)
    occurrences: dict[str, list[_NodeOccurrence]] = {}
    context_snapshots: dict[str, list[_ContextSnapshot]] = {}
    conversation_occurrences = 0

    for path in ordered_paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Claude.ai export JSON in {path}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"Claude.ai conversations export must be an array in {path}")
        for conversation_index, conversation in enumerate(raw):
            if not isinstance(conversation, dict):
                raise ValueError(
                    f"Claude.ai conversation {conversation_index} must be an object in {path}"
                )
            context_id = _required_string(
                conversation.get("uuid"),
                f"Claude.ai conversation {conversation_index} uuid",
                path,
            )
            messages = conversation.get("chat_messages")
            if not isinstance(messages, list):
                raise ValueError(
                    f"Claude.ai conversation {context_id!r} chat_messages must be an array in {path}"
                )
            conversation_occurrences += 1
            context_node_ids: set[str] = set()
            context_times: list[str] = []
            snapshot_occurrences: list[_NodeOccurrence] = []
            for message_index, item in enumerate(messages):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Claude.ai message {message_index} in {context_id!r} must be an object"
                    )
                occurrence = _normalize_message(item, path, context_id, message_index)
                snapshot_occurrences.append(occurrence)
                context_node_ids.add(occurrence.native_node_id)
                if occurrence.occurred_at:
                    context_times.append(occurrence.occurred_at)

            observed_at = _optional_timestamp(
                conversation.get("updated_at") or conversation.get("created_at"),
                path,
                f"conversation {context_id!r}",
            )
            if observed_at is None and context_times:
                observed_at = max(context_times)
            context_snapshots.setdefault(context_id, []).append(
                _ContextSnapshot(
                    native_context_id=context_id,
                    observed_at=observed_at,
                    native_node_ids=frozenset(context_node_ids),
                    occurrences=tuple(snapshot_occurrences),
                    path=path,
                    index=conversation_index,
                )
            )

    selected_snapshots: dict[str, _ContextSnapshot] = {}
    for context_id, snapshots in context_snapshots.items():
        snapshot = _select_context_snapshot(context_id, snapshots)
        selected_snapshots[context_id] = snapshot
        for occurrence in snapshot.occurrences:
            occurrences.setdefault(occurrence.native_node_id, []).append(occurrence)

    selected: dict[str, _NodeOccurrence] = {}
    for node_id, group in occurrences.items():
        baseline = group[0].immutable_facts()
        if any(item.immutable_facts() != baseline for item in group[1:]):
            raise ValueError(
                f"Claude.ai message UUID {node_id!r} has conflicting portable facts"
            )
        selected[node_id] = group[0]

    effective_parents: dict[str, str | None] = {}
    detached_parent_count = 0
    for node_id, item in selected.items():
        parent_id = item.raw_parent_id
        if parent_id is not None and parent_id not in selected:
            detached_parent_count += 1
            parent_id = None
        effective_parents[node_id] = parent_id
    _assert_acyclic(effective_parents)

    nodes: list[dict[str, Any]] = []
    for node_id, item in selected.items():
        node: dict[str, Any] = {
            "native_node_id": node_id,
            "native_parent_node_id": effective_parents[node_id],
            "occurred_at": item.occurred_at,
            "kind": item.kind,
            "source_type": item.source_type,
        }
        if item.message is not None:
            node["message"] = item.message
        nodes.append(node)
    nodes.sort(key=lambda item: (item.get("occurred_at") or "", item["native_node_id"]))

    observations: list[dict[str, Any]] = []
    skipped_contexts_without_time = 0
    for context_id, snapshot in sorted(selected_snapshots.items()):
        if not snapshot.native_node_ids or snapshot.observed_at is None:
            skipped_contexts_without_time += 1
            continue
        roots = {
            _root_for(node_id, effective_parents)
            for node_id in snapshot.native_node_ids
        }
        for root_id in sorted(roots):
            observations.append({
                "native_context_id": context_id,
                "kind": ARCHIVE_OBSERVATION_KIND,
                "native_node_id": root_id,
                "observed_at": snapshot.observed_at,
                "payload": {"archive": "claude-ai"},
            })

    occurred = [item.occurred_at for item in selected.values() if item.occurred_at]
    envelope = {
        "format": "neroli-conversation-tree-v1",
        "source": source,
        "room": room,
        "nodes": nodes,
        "observations": observations,
    }
    return ClaudeAiTreeExport(
        envelope=envelope,
        report={
            "files": len(ordered_paths),
            "conversation_occurrences": conversation_occurrences,
            "contexts": len(context_snapshots),
            "superseded_snapshots": conversation_occurrences - len(selected_snapshots),
            "nodes": len(nodes),
            "roots": sum(parent is None for parent in effective_parents.values()),
            "detached_parent_roots": detached_parent_count,
            "observations": len(observations),
            "contexts_without_observation": skipped_contexts_without_time,
            "first_occurred_at": min(occurred) if occurred else None,
            "last_occurred_at": max(occurred) if occurred else None,
        },
    )


def export_claude_ai_tree(
    paths: Iterable[Path],
    *,
    room: str,
    source: str = SOURCE,
) -> dict[str, Any]:
    return build_claude_ai_tree(paths, room=room, source=source).envelope


def _normalize_message(
    item: dict[str, Any], path: Path, context_id: str, index: int
) -> _NodeOccurrence:
    node_id = _required_string(
        item.get("uuid"),
        f"Claude.ai message {index} in {context_id!r} uuid",
        path,
    )
    parent_id = _optional_string(item.get("parent_message_uuid"))
    occurred_at = _optional_timestamp(
        item.get("created_at") or item.get("updated_at"),
        path,
        f"message {node_id!r}",
    )
    sender = _optional_string(item.get("sender")) or "unknown"
    role = {"human": "user", "assistant": "assistant"}.get(sender)
    content = _portable_content(item)
    message = None
    if role is not None and content:
        kind = "message"
        message = {"role": role, "content": content}
    else:
        kind = "event"
    return _NodeOccurrence(
        native_node_id=node_id,
        raw_parent_id=parent_id,
        occurred_at=occurred_at,
        kind=kind,
        source_type=f"chat_message:{sender}",
        message=message,
    )


def _select_context_snapshot(
    context_id: str, snapshots: list[_ContextSnapshot]
) -> _ContextSnapshot:
    """Choose one authoritative full snapshot without inventing edit history."""
    latest_time = max(snapshot.observed_at or "" for snapshot in snapshots)
    latest = [
        snapshot for snapshot in snapshots
        if (snapshot.observed_at or "") == latest_time
    ]
    largest_size = max(len(snapshot.native_node_ids) for snapshot in latest)
    finalists = [
        snapshot for snapshot in latest
        if len(snapshot.native_node_ids) == largest_size
    ]
    baseline = _snapshot_facts(finalists[0])
    if any(_snapshot_facts(snapshot) != baseline for snapshot in finalists[1:]):
        raise ValueError(
            f"Claude.ai conversation {context_id!r} has ambiguous equally recent snapshots"
        )
    chosen = min(finalists, key=lambda item: (str(item.path), item.index))
    all_captured_nodes = set().union(
        *(snapshot.native_node_ids for snapshot in snapshots)
    )
    missing = all_captured_nodes - chosen.native_node_ids
    if missing:
        raise ValueError(
            f"latest Claude.ai snapshot for conversation {context_id!r} omits earlier nodes"
        )
    return chosen


def _snapshot_facts(snapshot: _ContextSnapshot) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted(
        (occurrence.native_node_id, *occurrence.immutable_facts())
        for occurrence in snapshot.occurrences
    ))


def _portable_content(item: dict[str, Any]) -> list[dict[str, str]]:
    content = item.get("content")
    blocks: list[dict[str, str]] = []
    if isinstance(content, list):
        blocks = [
            {"type": "text", "text": block["text"]}
            for block in content
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        ]
    if not any(block["text"].strip() for block in blocks):
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            blocks = [{"type": "text", "text": text}]
    return blocks if any(block["text"].strip() for block in blocks) else []


def _root_for(node_id: str, parents: dict[str, str | None]) -> str:
    current = node_id
    while parents[current] is not None:
        current = parents[current]  # type: ignore[assignment]
    return current


def _assert_acyclic(parents: dict[str, str | None]) -> None:
    complete: set[str] = set()
    for start in parents:
        current: str | None = start
        path: set[str] = set()
        while current is not None and current not in complete:
            if current in path:
                raise ValueError(f"Claude.ai archive contains a parent cycle at {current!r}")
            path.add(current)
            current = parents.get(current)
        complete.update(path)


def _optional_timestamp(value: Any, path: Path, where: str) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid Claude.ai timestamp for {where} in {path}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Claude.ai timestamp for {where} must include a timezone in {path}")
    return parsed.astimezone(dt.UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _required_string(value: Any, label: str, path: Path) -> str:
    text = _optional_string(value)
    if text is None:
        raise ValueError(f"{label} must be a non-empty string in {path}")
    return text


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
