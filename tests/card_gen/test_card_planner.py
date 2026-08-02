"""Characterization tests for the schema-free Card Planner seam."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
from gen_cards import update_session_cards  # noqa: E402
from memory_types import Message  # noqa: E402


class StubModel:
    name = "fake:planner"


def _messages(session_id: str, lo: int, hi: int) -> list[Message]:
    ts = datetime.now(timezone.utc).isoformat()
    return [
        Message(
            role="user", speaker="user", text=f"round {round_no}", timestamp=ts,
            session_id=session_id, seq=round_no, round=round_no, label=str(round_no),
            source_uuid=f"{session_id}-{round_no}", message_seq=1, line_no=round_no,
        )
        for round_no in range(lo, hi + 1)
    ]


class CardPlannerCharacterizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "test.db")
        self.session_id = "abcdefgh-session"
        db.ingest_turns(self.conn, _messages(self.session_id, 1, 7))
        for index, (lo, hi, label) in enumerate(
            [(1, 2, "a"), (2, 4, "b"), (4, 6, "c")], start=1
        ):
            db.insert_card(
                self.conn,
                {
                    "card_id": f"abcdefgh#{index}",
                    "session_id": self.session_id,
                    "turns": [lo, hi],
                    "headline": f"{label} headline",
                    "share": f"{label} share",
                    "private": "",
                    "tags": [label],
                    "room": "main",
                    "model": "old:model",
                    "raw": f"{label} raw",
                },
                label="test",
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_update_refeeds_inclusive_boundary_and_replaces_only_owned_tail(self) -> None:
        replacement = {
            "turns": [4, 7],
            "headline": "new tail",
            "share": "new share",
            "private": "",
            "tags": ["new"],
            "raw": "new raw",
        }
        model = StubModel()
        run_id = db.create_pipeline_run(self.conn, model.name, None, [])

        with patch("gen_cards.generate", return_value=[replacement]) as generate:
            update_session_cards(
                self.conn, run_id, self.session_id, model, room="main"
            )

        context_messages = generate.call_args.args[0]
        self.assertEqual(
            sorted({message.round for message in context_messages}), [4, 5, 6, 7]
        )
        self.assertEqual(generate.call_args.kwargs["prior_summary"], "a share\n\nb share")

        stored = [
            tuple(row)
            for row in self.conn.execute(
                """
                SELECT card_id, headline, turn_start, turn_end
                FROM cards
                WHERE session_id = ?
                ORDER BY card_id
                """,
                (self.session_id,),
            ).fetchall()
        ]
        self.assertEqual(
            stored,
            [
                ("abcdefgh#1", "a headline", 1, 2),
                ("abcdefgh#2", "b headline", 2, 4),
                ("abcdefgh#3", "new tail", 4, 7),
            ],
        )


if __name__ == "__main__":
    unittest.main()
