from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_claude_ai_history  # noqa: E402
import db  # noqa: E402
import loaders  # noqa: E402
from claude_ai_adapter import build_claude_ai_tree  # noqa: E402
from history import list_history_contexts, render_history  # noqa: E402


def conversation(
    *, answer: str = " answer ", updated_at: str = "2026-01-01T00:00:04Z"
) -> dict:
    return {
        "uuid": "conversation-1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": updated_at,
        "name": "not-copied-to-canonical-history",
        "chat_messages": [
            {
                "uuid": "root-a",
                "parent_message_uuid": "absent-parent-a",
                "sender": "human",
                "created_at": "2026-01-01T00:00:01Z",
                "content": [{"type": "text", "text": " question "}],
                "attachments": [{"private": "not copied"}],
            },
            {
                "uuid": "left",
                "parent_message_uuid": "root-a",
                "sender": "assistant",
                "created_at": "2026-01-01T00:00:02Z",
                "content": [{"type": "text", "text": answer}],
            },
            {
                "uuid": "right",
                "parent_message_uuid": "root-a",
                "sender": "assistant",
                "created_at": "2026-01-01T00:00:03Z",
                "content": [{"type": "text", "text": "other branch"}],
            },
            {
                "uuid": "root-b",
                "parent_message_uuid": "absent-parent-b",
                "sender": "human",
                "created_at": "2026-01-01T00:00:04Z",
                "text": "detached question",
                "content": [],
            },
        ],
    }


class ClaudeAiTreeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, payload: list[dict]) -> Path:
        path = self.origin / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_preserves_explicit_tree_and_records_archive_roots_without_cursor(self) -> None:
        first = self._write("first-conversations.json", [conversation()])
        second = self._write("second-conversations.json", [conversation()])

        exported = build_claude_ai_tree([second, first], room="den")
        envelope = exported.envelope
        nodes = {item["native_node_id"]: item for item in envelope["nodes"]}

        self.assertEqual("claude-ai", envelope["source"])
        self.assertIsNone(nodes["root-a"]["native_parent_node_id"])
        self.assertIsNone(nodes["root-b"]["native_parent_node_id"])
        self.assertEqual("root-a", nodes["left"]["native_parent_node_id"])
        self.assertEqual("root-a", nodes["right"]["native_parent_node_id"])
        self.assertEqual(" question ", nodes["root-a"]["message"]["content"][0]["text"])
        self.assertNotIn("attachments", nodes["root-a"]["message"])
        self.assertEqual(4, exported.report["nodes"])
        self.assertEqual(2, exported.report["detached_parent_roots"])
        self.assertEqual(
            {("conversation-1", "archive-root", "root-a"),
             ("conversation-1", "archive-root", "root-b")},
            {
                (item["native_context_id"], item["kind"], item["native_node_id"])
                for item in envelope["observations"]
            },
        )

    def test_changed_native_message_is_a_hard_conflict(self) -> None:
        first = self._write("first.json", [conversation(answer="first")])
        second = self._write("second.json", [conversation(answer="changed")])

        with self.assertRaisesRegex(ValueError, "ambiguous equally recent snapshots"):
            build_claude_ai_tree([first, second], room="den")

    def test_latest_complete_conversation_snapshot_wins_over_older_content(self) -> None:
        older = self._write("older.json", [conversation(
            answer="older answer",
            updated_at="2026-01-01T00:00:04Z",
        )])
        newer = self._write("newer.json", [conversation(
            answer="newer answer",
            updated_at="2026-01-02T00:00:00Z",
        )])

        exported = build_claude_ai_tree([newer, older], room="den")
        nodes = {
            item["native_node_id"]: item for item in exported.envelope["nodes"]
        }
        self.assertEqual(
            "newer answer",
            nodes["left"]["message"]["content"][0]["text"],
        )
        self.assertEqual(1, exported.report["superseded_snapshots"])

    def test_latest_snapshot_may_not_silently_drop_older_nodes(self) -> None:
        older = conversation(updated_at="2026-01-01T00:00:04Z")
        newer = conversation(updated_at="2026-01-02T00:00:00Z")
        newer["chat_messages"] = newer["chat_messages"][:-1]
        older_path = self._write("older.json", [older])
        newer_path = self._write("newer.json", [newer])

        with self.assertRaisesRegex(ValueError, "omits earlier nodes"):
            build_claude_ai_tree([older_path, newer_path], room="den")

    def test_history_only_backfill_is_idempotent_and_groups_all_roots(self) -> None:
        self._write("conversations.json", [conversation()])
        database = self.root / "history.db"

        dry_output = io.StringIO()
        with redirect_stdout(dry_output):
            self.assertEqual(
                0,
                backfill_claude_ai_history.main([
                    "--origin-data", str(self.origin),
                    "--room", "den",
                    "--db", str(database),
                ]),
            )
        self.assertFalse(database.exists())
        self.assertEqual("dry-run", json.loads(dry_output.getvalue())["mode"])

        with mock.patch.object(
            loaders,
            "validate_source_room",
            side_effect=lambda _source, room: room,
        ):
            for _ in range(2):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        0,
                        backfill_claude_ai_history.main([
                            "--origin-data", str(self.origin),
                            "--room", "den",
                            "--db", str(database),
                            "--apply",
                        ]),
                    )

        conn = db.connect(database)
        self.assertEqual(4, conn.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0])
        self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM conversation_observations").fetchone()[0])
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        self.assertEqual(
            0,
            conn.execute(
                "SELECT revision FROM change_watermarks WHERE name = 'turns'"
            ).fetchone()[0],
        )

        catalog = list_history_contexts(conn, source="claude-ai")
        self.assertEqual(1, len(catalog["contexts"]))
        self.assertEqual("archive-root", catalog["contexts"][0]["kind"])
        self.assertEqual(4, catalog["contexts"][0]["path_nodes"])
        rendered = render_history(
            conn,
            room="den",
            source="claude-ai",
            native_context_id="conversation-1",
        )
        self.assertEqual(4, len(rendered["nodes"]))
        self.assertEqual(2, len(rendered["roots"]))
        self.assertTrue(rendered["observations"])
        self.assertTrue(all(not item["path"] for item in rendered["observations"]))
        conn.close()


if __name__ == "__main__":
    unittest.main()
