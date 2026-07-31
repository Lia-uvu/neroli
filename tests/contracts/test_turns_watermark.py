import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db
from memory_types import Message


def message(uuid: str, *, round_no: int = 1, line_no: int = 1) -> Message:
    return Message(
        role="user",
        speaker="User",
        text=f"text-{uuid}",
        timestamp="2026-07-31T12:00:00Z",
        session_id="session-1",
        seq=line_no,
        round=round_no,
        label=uuid,
        source_uuid=uuid,
        message_seq=1,
        source_file="/tmp/source.jsonl",
        line_no=line_no,
    )


class TurnsWatermarkTest(unittest.TestCase):
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

    def test_actual_turn_changes_increment_but_idempotent_ingest_does_not(self) -> None:
        self.assertEqual(self.revision(), 0)

        db.ingest_turns(self.conn, [message("a"), message("b", round_no=2, line_no=2)])
        self.assertEqual(self.revision(), 2)

        # ingest_turns uses ON CONFLICT DO UPDATE; identical values must not wake Card Gen.
        db.ingest_turns(self.conn, [message("a"), message("b", round_no=2, line_no=2)])
        self.assertEqual(self.revision(), 2)

        self.conn.execute(
            "UPDATE turns SET round=3 WHERE session_id='session-1' AND source_uuid='b'"
        )
        self.conn.commit()
        self.assertEqual(self.revision(), 3)

        self.conn.execute(
            "DELETE FROM turns WHERE session_id='session-1' AND source_uuid='a'"
        )
        self.conn.commit()
        self.assertEqual(self.revision(), 4)

    def test_direct_adapter_insert_uses_same_watermark(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages
            (source_uuid, role, speaker, text, timestamp, source)
            VALUES ('future-source:a', 'user', 'User', 'hello',
                    '2026-07-31T12:00:00Z', 'future-adapter')
            """
        )
        self.conn.execute(
            """
            INSERT INTO turns
            (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES ('future-session', 'future-source:a', 1, 1, NULL, NULL)
            """
        )
        self.conn.commit()
        self.assertEqual(self.revision(), 1)

    def test_derived_card_writes_do_not_increment_turns_watermark(self) -> None:
        db.ingest_turns(self.conn, [message("a")])
        self.assertEqual(self.revision(), 1)
        self.conn.execute(
            """
            INSERT INTO cards
            (card_id, session_id, turn_start, turn_end, headline,
             share, private, timestamp, room, model)
            VALUES ('session-1#1', 'session-1', 1, 1, 'headline',
                    'share', '', '2026-07-31T12:00:00Z', 'room', 'fake')
            """
        )
        self.conn.execute(
            "INSERT INTO card_tags (card_id, tag) VALUES ('session-1#1', 'test')"
        )
        self.conn.commit()
        self.assertEqual(self.revision(), 1)

    def test_v9_through_v11_migrations_preserve_turns_and_install_contract(self) -> None:
        db_path = Path(self.tmp.name) / "migration.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE messages (
              source_uuid TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              speaker TEXT,
              text TEXT NOT NULL,
              timestamp TEXT,
              parent_uuid TEXT,
              source TEXT,
              model TEXT,
              has_image INTEGER DEFAULT 0,
              image_count INTEGER DEFAULT 0,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE turns (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              source_uuid TEXT NOT NULL REFERENCES messages(source_uuid),
              round INTEGER NOT NULL,
              message_seq INTEGER DEFAULT 1,
              source_file TEXT,
              line_no INTEGER,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(session_id, source_uuid)
            );
            INSERT INTO messages
              (source_uuid, role, speaker, text, timestamp, source)
            VALUES
              ('existing', 'user', 'User', 'kept', '2026-07-30T12:00:00Z', 'fixture');
            INSERT INTO turns
              (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES
              ('existing-session', 'existing', 1, 1, '/fixture/source.jsonl', 1);
            PRAGMA user_version = 9;
            """
        )
        migration = (ROOT / "migrations" / "010-turns-watermark.sql").read_text(
            encoding="utf-8"
        )
        conn.executescript(migration)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
        self.assertEqual(
            conn.execute(
                "SELECT revision FROM change_watermarks WHERE name='turns'"
            ).fetchone()[0],
            0,
        )

        conn.execute(
            """
            INSERT INTO messages
              (source_uuid, role, speaker, text, timestamp, source)
            VALUES
              ('new', 'user', 'User', 'new', '2026-07-31T12:00:00Z', 'fixture')
            """
        )
        conn.execute(
            """
            INSERT INTO turns
              (session_id, source_uuid, round, message_seq, source_file, line_no)
            VALUES
              ('new-session', 'new', 1, 1, '/fixture/new.jsonl', 1)
            """
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT revision FROM change_watermarks WHERE name='turns'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        migration = (
            ROOT / "migrations" / "011-canonical-adapter-contract.sql"
        ).read_text(encoding="utf-8")
        conn.executescript(migration)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(
            [row[1] for row in conn.execute("PRAGMA table_info(messages)")][-3:],
            ["provider", "native_message_id", "native_parent_message_id"],
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_sessions").fetchone()[0], 0)
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
