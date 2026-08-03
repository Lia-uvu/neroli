from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import loaders  # noqa: E402
import pipeline  # noqa: E402
from gen_cards import update_session_cards  # noqa: E402
from history import render_history  # noqa: E402


class StubModel:
    name = "fake:tree-planner"


def envelope(*, cursor: str = "left", observed_at: str = "2026-08-03T12:00:00Z") -> dict:
    return {
        "format": "neroli-conversation-tree-v1",
        "source": "fake-tree",
        "room": "den",
        "nodes": [
            {
                "native_node_id": "root",
                "native_parent_node_id": None,
                "occurred_at": "2026-08-03T11:59:00Z",
                "kind": "message",
                "source_type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "question"}],
                },
            },
            {
                "native_node_id": "left",
                "native_parent_node_id": "root",
                "occurred_at": "2026-08-03T11:59:01Z",
                "kind": "message",
                "source_type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "left answer"}],
                    "provider": "fake-provider",
                    "model": "fake-model",
                },
            },
            {
                "native_node_id": "right",
                "native_parent_node_id": "root",
                "occurred_at": "2026-08-03T11:59:02Z",
                "kind": "message",
                "source_type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "right answer"}],
                },
            },
        ],
        "observations": [
            {
                "native_context_id": "runtime-session",
                "kind": "cursor",
                "native_node_id": cursor,
                "observed_at": observed_at,
                "payload": {"runtime": "fake"},
            }
        ],
    }


class ConversationTreeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "tree.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def load(self, payload: dict):
        with mock.patch.object(loaders, "validate_source_room", return_value="den"):
            return loaders.load_conversation_tree(payload, self.root / "tree.json")

    def revision(self) -> int:
        return self.conn.execute(
            "SELECT revision FROM change_watermarks WHERE name='turns'"
        ).fetchone()[0]

    def test_whole_tree_cards_both_branches_but_cursor_only_changes_rendering(self) -> None:
        batch = self.load(envelope())
        branches = db.ingest_conversation_tree(self.conn, batch)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0],
            3,
        )
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
            4,
        )
        branch_rows = self.conn.execute(
            """
            SELECT t.session_id, n.native_node_id, t.is_context
            FROM turns t JOIN conversation_nodes n ON n.node_id = t.source_uuid
            ORDER BY t.session_id, t.line_no
            """
        ).fetchall()
        self.assertEqual(
            sorted((row["native_node_id"], row["is_context"]) for row in branch_rows),
            [("left", 0), ("right", 0), ("root", 0), ("root", 1)],
        )

        before = self.revision()
        moved = self.load(envelope(cursor="right", observed_at="2026-08-03T12:05:00Z"))
        self.assertEqual(db.ingest_conversation_tree(self.conn, moved), [])
        self.assertEqual(self.revision(), before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversation_observations").fetchone()[0],
            2,
        )

        rendered = render_history(
            self.conn, room="den", source="fake-tree",
            native_context_id="runtime-session",
        )
        self.assertEqual(len(rendered["nodes"]), 3)
        self.assertEqual(len(rendered["observations"]), 1)
        latest = rendered["observations"][0]
        self.assertEqual(latest["native_node_id"], "right")
        native_by_node = {
            node["node_id"]: node["native_node_id"] for node in rendered["nodes"]
        }
        self.assertEqual(
            [native_by_node[node_id] for node_id in latest["path"]],
            ["root", "right"],
        )

    def test_card_ownership_never_duplicates_the_shared_ancestor(self) -> None:
        db.ingest_conversation_tree(self.conn, self.load(envelope()))
        owner_rows = self.conn.execute(
            """
            SELECT n.native_node_id, owned.session_id
            FROM conversation_nodes n
            JOIN conversation_node_branches owned ON owned.node_id = n.node_id
            WHERE n.kind = 'message'
            """
        ).fetchall()
        owner = {row["native_node_id"]: row["session_id"] for row in owner_rows}
        parent_session = owner["root"]
        self.assertEqual(owner["left"], parent_session)
        child_session = owner["right"]
        self.assertNotEqual(child_session, parent_session)

        model = StubModel()
        parent_run = db.create_pipeline_run(self.conn, model.name, None, [])
        parent_card = {
            "turns": [1, 1], "headline": "left", "share": "left memory",
            "private": "", "tags": ["left"], "raw": "left raw",
        }
        with patch("gen_cards.generate", return_value=[parent_card]):
            update_session_cards(
                self.conn, parent_run, parent_session, model, room="den"
            )

        child_run = db.create_pipeline_run(self.conn, model.name, None, [])
        child_card = {
            "turns": [1, 1], "headline": "right", "share": "right memory",
            "private": "", "tags": ["right"], "raw": "right raw",
        }
        with patch("gen_cards.generate", return_value=[child_card]):
            update_session_cards(
                self.conn, child_run, child_session, model, room="den"
            )

        coverage = self.conn.execute(
            """
            SELECT n.native_node_id, COUNT(*) AS uses
            FROM card_nodes covered
            JOIN conversation_nodes n ON n.node_id = covered.node_id
            GROUP BY n.native_node_id
            ORDER BY n.native_node_id
            """
        ).fetchall()
        self.assertEqual(
            [(row["native_node_id"], row["uses"]) for row in coverage],
            [("left", 1), ("right", 1), ("root", 1)],
        )

    def test_tree_first_card_policy_is_source_local(self) -> None:
        db.ingest_conversation_tree(self.conn, self.load(envelope()))
        settings = {
            "card_gen": {
                "min_first_session_turns": 3,
                "min_first_session_turns_exempt_sources": ["fake-tree"],
            }
        }
        with patch.object(pipeline, "load_settings", return_value=settings):
            selected = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}
        self.assertEqual(
            selected,
            {
                row["session_id"]
                for row in self.conn.execute(
                    "SELECT session_id FROM conversation_card_branches"
                ).fetchall()
            },
        )

    def test_missing_parent_and_node_mutation_are_hard_failures(self) -> None:
        bad = envelope()
        bad["nodes"][0]["native_parent_node_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "missing parent"):
            db.ingest_conversation_tree(self.conn, self.load(bad))

        db.ingest_conversation_tree(self.conn, self.load(envelope()))
        changed = envelope()
        changed["nodes"][1]["message"]["content"][0]["text"] = "mutated"
        with self.assertRaisesRegex(ValueError, "immutable conversation node conflict"):
            db.ingest_conversation_tree(self.conn, self.load(changed))

    def test_canonical_message_keeps_portable_blocks_exactly(self) -> None:
        payload = envelope()
        payload["nodes"] = [payload["nodes"][0]]
        payload["observations"] = []
        payload["nodes"][0]["message"]["content"] = [
            {"type": "text", "text": "  first block\n"},
            {"type": "text", "text": "second block  "},
        ]

        db.ingest_conversation_tree(self.conn, self.load(payload))
        row = self.conn.execute(
            "SELECT text, message_json FROM conversation_nodes"
        ).fetchone()
        self.assertEqual(row["text"], "first block\n\nsecond block")
        self.assertEqual(
            json.loads(row["message_json"])["content"],
            payload["nodes"][0]["message"]["content"],
        )

    def test_latest_observation_uses_canonical_utc_order(self) -> None:
        db.ingest_conversation_tree(
            self.conn,
            self.load(envelope(cursor="left", observed_at="2026-08-03T12:00:00Z")),
        )
        db.ingest_conversation_tree(
            self.conn,
            self.load(
                envelope(cursor="right", observed_at="2026-08-03T12:00:00.500Z")
            ),
        )

        rendered = render_history(
            self.conn,
            room="den",
            source="fake-tree",
            native_context_id="runtime-session",
        )
        self.assertEqual(rendered["observations"][0]["native_node_id"], "right")
        self.assertEqual(
            rendered["observations"][0]["observed_at"],
            "2026-08-03T12:00:00.500000Z",
        )


if __name__ == "__main__":
    unittest.main()
