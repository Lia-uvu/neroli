"""Source adapters are peers: stopping one must not block another or downstream wakeups.

This contract deliberately knows nothing about Claude Code, Porch, or any other
adapter implementation. It uses fake sources and a temporary Neroli database.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
from memory_types import Message  # noqa: E402


def message(source: str, ordinal: int) -> Message:
    return Message(
        role="user",
        speaker="User",
        text=f"{source}-message-{ordinal}",
        timestamp=f"2026-07-31T12:00:{ordinal:02d}Z",
        session_id=f"{source}-session",
        seq=ordinal,
        round=ordinal,
        label=str(ordinal),
        source_uuid=f"{source}:{ordinal}",
        message_seq=1,
        source_file=f"/fake/{source}/session.jsonl",
        line_no=ordinal,
        source=source,
    )


class SourceIndependenceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def revision(self) -> int:
        return self.conn.execute(
            "SELECT revision FROM change_watermarks WHERE name='turns'"
        ).fetchone()[0]

    def source_rows(self) -> list[tuple[str, str]]:
        return [
            (row["source"], row["text"])
            for row in self.conn.execute(
                "SELECT source, text FROM messages ORDER BY source, source_uuid"
            )
        ]

    def test_one_source_can_continue_while_the_other_is_silent(self) -> None:
        db.ingest_turns(self.conn, [message("source-a", 1)])
        db.ingest_turns(self.conn, [message("source-b", 1)])
        self.assertEqual(self.revision(), 2)

        # source-a is now "off": it simply submits nothing. source-b remains independent.
        changed = db.ingest_turns(self.conn, [message("source-b", 2)])

        self.assertEqual(changed, ["source-b-session"])
        self.assertEqual(self.revision(), 3)
        self.assertEqual(
            self.source_rows(),
            [
                ("source-a", "source-a-message-1"),
                ("source-b", "source-b-message-1"),
                ("source-b", "source-b-message-2"),
            ],
        )

    def test_idempotent_retry_from_one_source_does_not_wake_or_rewrite_the_other(self) -> None:
        source_a = message("source-a", 1)
        source_b = message("source-b", 1)
        db.ingest_turns(self.conn, [source_a, source_b])
        baseline_revision = self.revision()
        baseline_b = self.conn.execute(
            """
            SELECT m.text, t.round, t.message_seq
            FROM messages m
            JOIN turns t USING (source_uuid)
            WHERE m.source = 'source-b'
            """
        ).fetchone()

        db.ingest_turns(self.conn, [source_a])

        self.assertEqual(self.revision(), baseline_revision)
        current_b = self.conn.execute(
            """
            SELECT m.text, t.round, t.message_seq
            FROM messages m
            JOIN turns t USING (source_uuid)
            WHERE m.source = 'source-b'
            """
        ).fetchone()
        self.assertEqual(tuple(current_b), tuple(baseline_b))

    def test_removing_one_source_keeps_the_other_and_wakes_by_db_change(self) -> None:
        db.ingest_turns(
            self.conn,
            [message("source-a", 1), message("source-b", 1)],
        )
        self.conn.execute(
            """
            DELETE FROM turns
            WHERE source_uuid IN (
                SELECT source_uuid FROM messages WHERE source = 'source-a'
            )
            """
        )
        self.conn.commit()

        self.assertEqual(self.revision(), 3)
        remaining = self.conn.execute(
            """
            SELECT m.source
            FROM turns t JOIN messages m USING (source_uuid)
            """
        ).fetchall()
        self.assertEqual([row["source"] for row in remaining], ["source-b"])


if __name__ == "__main__":
    unittest.main()
