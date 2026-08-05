#!/usr/bin/env python3
"""Provider-free Claude Code tree-adapter compatibility audit.

Reads the configured native JSONL journals, compares every legacy Card-input
turn with the new portable tree message, and replays the tree twice into an
isolated temporary Neroli database.  It never prints conversation bodies and
never calls a model.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import loaders  # noqa: E402
from claude_code_adapter import (  # noqa: E402
    _source_parent_overrides,
    export_claude_code_tree,
)
from config import PROJECT_DIRS, ROOMS  # noqa: E402


def main() -> int:
    report: dict[str, object] = {"rooms": {}}
    total_missing = total_tree_only = total_role_mismatch = total_text_mismatch = 0
    total_parent_mismatch = 0
    expected_nodes = expected_observations = 0
    with tempfile.TemporaryDirectory(prefix="neroli-claude-tree-audit-") as tmp:
        conn = db.connect(Path(tmp) / "audit.db")
        before_revision = _turn_revision(conn)
        for room, project_dir in zip(ROOMS, PROJECT_DIRS):
            paths = sorted(project_dir.glob("*.jsonl"))
            envelope = export_claude_code_tree(paths, room=room)
            expected_parents, structural_counts = _expected_parent_overrides(paths)
            tree_by_id = {
                node["native_node_id"]: node for node in envelope["nodes"]
            }
            parent_mismatches = sum(
                tree_by_id[node_id]["native_parent_node_id"] != parent_id
                for node_id, parent_id in expected_parents.items()
            )
            tree_messages = {
                node["native_node_id"]: node
                for node in envelope["nodes"]
                if node["kind"] == "message"
            }
            with contextlib.redirect_stderr(io.StringIO()):
                legacy = loaders.load_messages_for_ingest(paths)
            legacy_ids = {message.source_uuid for message in legacy}
            tree_only = len(set(tree_messages) - legacy_ids)
            missing = role_mismatch = text_mismatch = 0
            for message in legacy:
                node = tree_messages.get(message.source_uuid)
                if node is None:
                    missing += 1
                    continue
                portable = node["message"]
                role_mismatch += portable["role"] != message.role
                text = "\n".join(
                    part["text"] for part in portable["content"]
                ).strip()
                text_mismatch += text != message.text

            batch = loaders.load_conversation_tree(
                envelope, Path(tmp) / f"claude-code-{room}.json"
            )
            db.ingest_conversation_tree(conn, batch, project_cards=False)
            db.ingest_conversation_tree(conn, batch, project_cards=False)
            report["rooms"][room] = {
                "files": len(paths),
                "legacy_turns": len(legacy),
                "tree_nodes": len(envelope["nodes"]),
                "tree_messages": len(tree_messages),
                "cursor_observations": len(envelope["observations"]),
                "legacy_missing_from_tree": missing,
                "tree_messages_missing_from_legacy": tree_only,
                "role_mismatches": role_mismatch,
                "text_mismatches": text_mismatch,
                "structural_normalization": {
                    **structural_counts,
                    "parent_mismatches": parent_mismatches,
                },
            }
            total_missing += missing
            total_tree_only += tree_only
            total_role_mismatch += role_mismatch
            total_text_mismatch += text_mismatch
            total_parent_mismatch += parent_mismatches
            expected_nodes += len(envelope["nodes"])
            expected_observations += len(envelope["observations"])

        isolated = {
            "stored_nodes": _count(conn, "conversation_nodes"),
            "stored_observations": _count(conn, "conversation_observations"),
            "stored_messages": _count(conn, "messages"),
            "stored_turns": _count(conn, "turns"),
            "stored_card_branches": _count(conn, "conversation_card_branches"),
            "model_calls": _count(conn, "model_calls"),
            "turn_watermark_before": before_revision,
            "turn_watermark_after": _turn_revision(conn),
            "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
        }
        report["isolated_replay"] = isolated
        conn.close()

    report["compatible"] = (
        not any((
            total_missing,
            total_tree_only,
            total_role_mismatch,
            total_text_mismatch,
            total_parent_mismatch,
        ))
        and isolated["stored_nodes"] == expected_nodes
        and isolated["stored_observations"] == expected_observations
        and isolated["stored_messages"] == 0
        and isolated["stored_turns"] == 0
        and isolated["stored_card_branches"] == 0
        and isolated["model_calls"] == 0
        and isolated["turn_watermark_before"] == isolated["turn_watermark_after"]
        and isolated["quick_check"] == "ok"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["compatible"] else 1


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _turn_revision(conn) -> int:
    return conn.execute(
        "SELECT revision FROM change_watermarks WHERE name='turns'"
    ).fetchone()[0]


def _expected_parent_overrides(
    paths: list[Path],
) -> tuple[dict[str, str], dict[str, int]]:
    expected: dict[str, str] = {}
    native_types: dict[str, tuple[str | None, str | None]] = {}
    for path in paths:
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    continue
                rows.append(raw)
                node_id = raw.get("uuid")
                if isinstance(node_id, str):
                    native_types[node_id] = (raw.get("type"), raw.get("subtype"))
        for node_id, parent_id in _source_parent_overrides(rows, path).items():
            previous = expected.get(node_id)
            if previous is not None and previous != parent_id:
                raise ValueError(
                    f"conflicting audit parent expectation for {node_id!r}"
                )
            expected[node_id] = parent_id
    return expected, {
        "normalized_parent_edges": len(expected),
        "api_retry_assistants": sum(
            native_types.get(node_id, (None, None))[0] == "assistant"
            for node_id in expected
        ),
        "api_retry_stop_hooks": sum(
            native_types.get(node_id) == ("system", "stop_hook_summary")
            for node_id in expected
        ),
        "compact_boundaries": sum(
            native_types.get(node_id) == ("system", "compact_boundary")
            for node_id in expected
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
