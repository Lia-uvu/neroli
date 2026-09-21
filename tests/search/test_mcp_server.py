from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import mcp_server  # noqa: E402


def card(
    card_id: str,
    *,
    room: str,
    headline: str,
    share: str = "",
    private: str = "",
    session: str = "session",
    turns: tuple[int, int] = (1, 2),
) -> dict:
    return {
        "card_id": card_id,
        "session_id": session,
        "turns": list(turns),
        "headline": headline,
        "share": share,
        "private": private,
        "timestamp": "2026-08-03T10:00:00Z",
        "room": room,
        "tags": [],
    }


class NeroliMcpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mcp.db"
        writable = db.connect(self.path)
        db.insert_card(
            writable,
            card(
                "den#1",
                room="den",
                headline="本房的礼物",
                share="可以分享的礼物",
                private="只有 den 能看的细节",
                turns=(1, 2),
            ),
        )
        db.insert_card(
            writable,
            card(
                "den#2",
                room="den",
                headline="本房后来",
                share="后续",
                private="后续私密",
                turns=(3, 4),
            ),
        )
        db.insert_card(
            writable,
            card(
                "loft#1",
                room="loft",
                headline="他房可见卡",
                share="公开的梧桐",
                private="隐藏的秘密",
                session="other",
            ),
        )
        writable.execute(
            """
            INSERT INTO messages
            (source_uuid, role, speaker, text, timestamp, source)
            VALUES ('den-message-1', 'user', 'Lia', '原始的礼物对话',
                    '2026-08-03T10:00:00Z', 'test')
            """
        )
        writable.execute(
            """
            INSERT INTO turns
            (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES ('session', 'den-message-1', 1, 1, NULL, NULL)
            """
        )
        writable.execute(
            """
            INSERT INTO messages
            (source_uuid, role, speaker, text, timestamp, source)
            VALUES ('loft-message-1', 'user', 'Lia', 'loft 原始对话',
                    '2026-08-03T10:00:00Z', 'test')
            """
        )
        writable.execute(
            """
            INSERT INTO turns
            (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES ('other', 'loft-message-1', 1, 1, NULL, NULL)
            """
        )
        db.insert_card(
            writable,
            card(
                "loft#2",
                room="loft",
                headline="他房不可见卡",
                private="绝对秘密",
                session="other",
            ),
        )
        writable.commit()
        writable.close()
        self.conn = mcp_server.connect_readonly(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def call(self, name: str, arguments: dict, *, default_viewer: str | None = None) -> dict:
        result = mcp_server.call_tool(self.conn, default_viewer, name, arguments)
        if result.get("isError"):
            return result
        return json.loads(result["content"][0]["text"])

    def test_server_connection_is_query_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("DELETE FROM cards")

    def test_caller_supplies_viewer_and_private_stays_room_local(self) -> None:
        listed = mcp_server.dispatch(
            self.conn,
            "den",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        search_schema = next(
            tool["inputSchema"]
            for tool in listed["result"]["tools"]
            if tool["name"] == "neroli_search"
        )
        self.assertIn("viewer", search_schema["properties"])
        self.assertIn("viewer", search_schema["required"])
        self.assertNotIn("room", search_schema["properties"])
        self.assertEqual(
            {tool["name"] for tool in listed["result"]["tools"]},
            {
                "neroli_search",
                "neroli_card",
                "neroli_session_context",
                "neroli_wander",
                "neroli_recent",
            },
        )

        own = self.call("neroli_card", {"viewer": "den", "card_id": "den#1"})
        self.assertEqual(own["private"], "只有 den 能看的细节")
        shared = self.call("neroli_card", {"viewer": "den", "card_id": "loft#1"})
        self.assertEqual(shared["share"], "公开的梧桐")
        self.assertEqual(shared["private"], "")
        hidden = mcp_server.call_tool(
            self.conn, None, "neroli_card", {"viewer": "den", "card_id": "loft#2"}
        )
        self.assertTrue(hidden["isError"])

        switched = self.call("neroli_card", {"viewer": "loft", "card_id": "loft#1"})
        self.assertEqual(switched["private"], "隐藏的秘密")

        outsider = self.call("neroli_card", {"viewer": "workshop", "card_id": "loft#1"})
        self.assertEqual(outsider["share"], "公开的梧桐")
        self.assertEqual(outsider["private"], "")

        with_turns = self.call(
            "neroli_card",
            {"viewer": "den", "card_id": "den#1", "include_turns": True},
        )
        self.assertEqual(with_turns["turns"][0]["text"], "原始的礼物对话")

        cross_room_turns = self.call(
            "neroli_card",
            {"viewer": "den", "card_id": "loft#1", "include_turns": True},
        )
        self.assertIsNone(cross_room_turns["turns"])
        self.assertIn("same-room", cross_room_turns["turns_unavailable"])

    def test_search_and_session_context_are_bounded(self) -> None:
        hits = self.call(
            "neroli_search",
            {"viewer": "den", "query": "礼物", "limit": 4, "expand": 1},
        )
        self.assertEqual([item["card_id"] for item in hits["cards"]], ["den#1"])
        self.assertEqual(hits["expanded_cards"][0]["private"], "只有 den 能看的细节")

        context = self.call(
            "neroli_session_context",
            {"viewer": "den", "card_id": "den#2", "limit": 2},
        )
        self.assertEqual(context["total_cards"], 2)
        self.assertEqual(
            [item["card_id"] for item in context["cards"]],
            ["den#1", "den#2"],
        )
        self.assertTrue(context["cards"][1]["selected"])
        self.assertEqual(
            [item["card_id"] for item in context["expanded_cards"]],
            ["den#1", "den#2"],
        )

        before = self.call(
            "neroli_session_context",
            {
                "viewer": "den",
                "card_id": "den#2",
                "direction": "before",
                "limit": 1,
                "expand": 1,
            },
        )
        self.assertEqual([item["card_id"] for item in before["cards"]], ["den#1"])
        self.assertEqual(
            [item["card_id"] for item in before["expanded_cards"]], ["den#1"]
        )

    def test_wander_and_recent_can_expand_results(self) -> None:
        wandered = self.call(
            "neroli_wander", {"viewer": "den", "limit": 2, "expand": 2}
        )
        self.assertEqual(len(wandered["cards"]), 2)
        self.assertEqual(len(wandered["expanded_cards"]), 2)

        recent = self.call(
            "neroli_recent",
            {
                "viewer": "den",
                "since": "2026-08-01",
                "until": "2026-08-04",
                "limit": 10,
                "expand": 1,
            },
        )
        self.assertEqual(recent["count"], 3)
        self.assertEqual(len(recent["expanded_cards"]), 1)

    def test_invalid_arguments_return_structured_tool_error(self) -> None:
        result = mcp_server.call_tool(
            self.conn,
            "den",
            "neroli_search",
            {"viewer": "den", "query": "x", "unexpected": True},
        )
        self.assertTrue(result["isError"])
        self.assertIn("unknown arguments", result["content"][0]["text"])

    def test_startup_viewer_is_only_a_compatibility_default(self) -> None:
        inherited = self.call("neroli_card", {"card_id": "den#1"}, default_viewer="den")
        self.assertEqual(inherited["private"], "只有 den 能看的细节")

        overridden = self.call(
            "neroli_card",
            {"viewer": "loft", "card_id": "loft#1"},
            default_viewer="den",
        )
        self.assertEqual(overridden["private"], "隐藏的秘密")

        missing = mcp_server.call_tool(
            self.conn, None, "neroli_card", {"card_id": "den#1"}
        )
        self.assertTrue(missing["isError"])
        self.assertIn("viewer must be", missing["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
