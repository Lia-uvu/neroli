from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import loaders  # noqa: E402


def envelope(*, text: str = "hello", native_session_id: str = "native-session") -> dict:
    return {
        "format": "neroli-normalized-v2",
        "source": "fake-adapter",
        "source_route": "resident-entry",
        "messages": [
            {
                "native_session_id": native_session_id,
                "native_message_id": "native-message",
                "role": "user",
                "text": text,
                "occurred_at": "2026-07-31T12:00:00Z",
                "source_sequence": 0,
            }
        ],
    }


class CanonicalAdapterContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def load(self, payload: dict):
        with mock.patch.object(loaders, "room_for_source_route", return_value="den"):
            return loaders.load_normalized_items(payload, self.root / "spool.json")

    def test_ingest_persists_native_provenance_and_policy_room(self) -> None:
        messages = self.load(envelope())
        db.ingest_turns(self.conn, messages)

        session = self.conn.execute("SELECT * FROM source_sessions").fetchone()
        stored = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(session["session_id"], messages[0].session_id)
        self.assertEqual(session["source"], "fake-adapter")
        self.assertEqual(session["native_session_id"], "native-session")
        self.assertEqual(session["source_route"], "resident-entry")
        self.assertEqual(session["room"], "den")
        self.assertEqual(stored["native_message_id"], "native-message")
        self.assertEqual(db.room_for_session(self.conn, messages[0].session_id), "den")

    def test_retry_is_idempotent_but_same_native_id_cannot_change_content(self) -> None:
        messages = self.load(envelope())
        db.ingest_turns(self.conn, messages)
        revision = self.conn.execute(
            "SELECT revision FROM change_watermarks WHERE name='turns'"
        ).fetchone()[0]

        db.ingest_turns(self.conn, self.load(envelope()))
        self.assertEqual(
            self.conn.execute(
                "SELECT revision FROM change_watermarks WHERE name='turns'"
            ).fetchone()[0],
            revision,
        )

        with self.assertRaisesRegex(ValueError, "immutable native message conflict"):
            db.ingest_turns(self.conn, self.load(envelope(text="changed")))
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute("SELECT text FROM messages").fetchone()[0], "hello"
        )


if __name__ == "__main__":
    unittest.main()
