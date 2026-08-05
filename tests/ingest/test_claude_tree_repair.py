from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import loaders  # noqa: E402
from ingest_claude_tree import _repair_history_only_nodes  # noqa: E402


def batch(nodes: list[dict]):
    return loaders.load_conversation_tree(
        {
            "format": "neroli-conversation-tree-v1",
            "source": "claude-code",
            "room": "den",
            "nodes": nodes,
            "observations": [],
        },
        Path("repair-test.json"),
    )


def node(node_id: str, parent_id: str | None, *, kind: str = "event") -> dict:
    item = {
        "native_node_id": node_id,
        "native_parent_node_id": parent_id,
        "occurred_at": "2026-01-01T00:00:00Z",
        "kind": kind,
        "source_type": "system" if kind == "event" else kind,
    }
    if kind == "message":
        item["message"] = {
            "role": "assistant",
            "content": [{"type": "text", "text": node_id}],
        }
    return item


class ClaudeTreeRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "repair.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_explicit_history_only_parent_repair_becomes_idempotent(self) -> None:
        old = batch([
            node("question", None, kind="message"),
            node("error", "question"),
            node("answer", "question", kind="message"),
            node("hook", "error"),
        ])
        desired = batch([
            node("question", None, kind="message"),
            node("error", "question"),
            node("answer", "error", kind="message"),
            node("hook", "answer"),
        ])
        db.ingest_conversation_tree(self.conn, old, project_cards=False)

        dry_run = _repair_history_only_nodes(
            self.conn,
            [desired],
            apply=False,
            allow_portable_noise=False,
            allow_parent_structure=True,
        )
        self.assertEqual(dry_run["parent_edges"], 2)
        self.assertEqual(dry_run["portable_nodes"], 0)

        applied = _repair_history_only_nodes(
            self.conn,
            [desired],
            apply=True,
            allow_portable_noise=False,
            allow_parent_structure=True,
        )
        self.assertEqual(applied["parent_edges"], 2)
        db.ingest_conversation_tree(self.conn, desired, project_cards=False)
        rows = self.conn.execute(
            """
            SELECT native_node_id, native_parent_node_id
            FROM conversation_nodes
            WHERE source = 'claude-code'
            ORDER BY native_node_id
            """
        ).fetchall()
        self.assertEqual(
            {row["native_node_id"]: row["native_parent_node_id"] for row in rows},
            {
                "answer": "error",
                "error": "question",
                "hook": "answer",
                "question": None,
            },
        )

    def test_parent_repair_refuses_card_projected_source(self) -> None:
        old = batch([
            node("question", None, kind="message"),
            node("answer", "question", kind="message"),
        ])
        desired = batch([
            node("question", None, kind="message"),
            node("answer", None, kind="message"),
        ])
        db.ingest_conversation_tree(self.conn, old, project_cards=True)

        with self.assertRaisesRegex(RuntimeError, "has Card projections"):
            _repair_history_only_nodes(
                self.conn,
                [desired],
                apply=False,
                allow_portable_noise=False,
                allow_parent_structure=True,
            )


if __name__ == "__main__":
    unittest.main()
