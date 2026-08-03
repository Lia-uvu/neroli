from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def call(self, name: str, arguments: dict) -> dict:
        result = mcp_server.call_tool(self.conn, "den", name, arguments)
        if result.get("isError"):
            return result
        return json.loads(result["content"][0]["text"])

    def test_server_connection_is_query_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("DELETE FROM cards")

    def test_viewer_is_not_a_tool_argument_and_private_stays_room_local(self) -> None:
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
        self.assertNotIn("viewer", search_schema["properties"])
        self.assertNotIn("room", search_schema["properties"])

        own = self.call("neroli_card", {"card_id": "den#1"})
        self.assertEqual(own["private"], "只有 den 能看的细节")
        shared = self.call("neroli_card", {"card_id": "loft#1"})
        self.assertEqual(shared["share"], "公开的梧桐")
        self.assertEqual(shared["private"], "")
        hidden = mcp_server.call_tool(
            self.conn, "den", "neroli_card", {"card_id": "loft#2"}
        )
        self.assertTrue(hidden["isError"])

    def test_search_and_session_context_are_bounded(self) -> None:
        hits = self.call("neroli_search", {"query": "礼物", "limit": 4})
        self.assertEqual([item["card_id"] for item in hits["cards"]], ["den#1"])

        context = self.call(
            "neroli_session_context", {"card_id": "den#2", "limit": 2}
        )
        self.assertEqual(context["total_cards"], 2)
        self.assertEqual(
            [item["card_id"] for item in context["cards"]],
            ["den#1", "den#2"],
        )
        self.assertTrue(context["cards"][1]["selected"])

    def test_invalid_arguments_return_structured_tool_error(self) -> None:
        result = mcp_server.call_tool(
            self.conn,
            "den",
            "neroli_search",
            {"query": "x", "viewer": "loft"},
        )
        self.assertTrue(result["isError"])
        self.assertIn("unknown arguments", result["content"][0]["text"])

    def test_viewer_must_be_configured(self) -> None:
        with patch.object(mcp_server, "ROOMS", ("den", "loft")), patch.object(
            mcp_server, "ROOM_SLUGS", {"den": "den", "loft": "loft"}
        ):
            self.assertEqual(mcp_server.validate_viewer("den"), "den")
            with self.assertRaisesRegex(ValueError, "unknown Neroli viewer"):
                mcp_server.validate_viewer("unknown")


if __name__ == "__main__":
    unittest.main()
