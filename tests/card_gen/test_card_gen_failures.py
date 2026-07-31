"""Card generation failure safety: preserve old cards and durably audit paid windows."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
from gen_cards import process_session_cards, update_session_cards  # noqa: E402
from memory_types import Message  # noqa: E402
from pipeline import auto_generate_cards, check_card_gen_threshold  # noqa: E402


def _messages(session_id: str, lo: int, hi: int) -> list[Message]:
    ts = datetime.now(timezone.utc).isoformat()
    return [
        Message(
            role="user", speaker="user", text=f"round {i}", timestamp=ts,
            session_id=session_id, seq=i, round=i, label=str(i),
            source_uuid=f"{session_id}-{i}", message_seq=1, line_no=i,
        )
        for i in range(lo, hi + 1)
    ]


class FakeModel:
    name = "fake:test"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def run(self, _prompt):
        item = self.outputs[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class CardGenFailureSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.sid = "abcdefgh-session"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _run_id(self, model):
        return db.create_pipeline_run(self.conn, model.name, None, [])

    def test_unparseable_update_preserves_old_tail_and_audits_raw(self):
        db.ingest_turns(self.conn, _messages(self.sid, 1, 3))
        good = FakeModel([
            "turns:1-3\nheadline:old tail\nshare:kept fact\nprivate:\ntags:test"
        ])
        process_session_cards(self.conn, self._run_id(good), self.sid, good, room="den")
        before = [tuple(r) for r in self.conn.execute(
            "SELECT card_id, headline FROM cards ORDER BY card_id"
        )]

        db.ingest_turns(self.conn, _messages(self.sid, 4, 4))
        bad_raw = "completed, but not in the required card format"
        bad = FakeModel([bad_raw])
        with self.assertRaisesRegex(RuntimeError, "no parseable cards"):
            update_session_cards(self.conn, self._run_id(bad), self.sid, bad, room="den")
        self.conn.rollback()

        after = [tuple(r) for r in self.conn.execute(
            "SELECT card_id, headline FROM cards ORDER BY card_id"
        )]
        self.assertEqual(after, before)
        audit = self.conn.execute(
            "SELECT step, raw_output FROM model_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["step"], "gen_cards_attempt_error")
        self.assertEqual(audit["raw_output"], bad_raw)
        ok, reason = check_card_gen_threshold(self.conn)
        self.assertFalse(ok)
        self.assertIn("too soon", reason)

    def test_late_window_failure_keeps_earlier_window_audit(self):
        db.ingest_turns(self.conn, _messages(self.sid, 1, 30))
        first = (
            "turns:1-7\nheadline:a\nshare:a\nprivate:\ntags:a\n\n"
            "turns:7-14\nheadline:b\nshare:b\nprivate:\ntags:b"
        )
        second = (
            "turns:7-20\nheadline:c\nshare:c\nprivate:\ntags:c\n\n"
            "turns:20-28\nheadline:d\nshare:d\nprivate:\ntags:d"
        )
        model = FakeModel([first, second, RuntimeError("third window failed")])
        with self.assertRaisesRegex(RuntimeError, "third window failed"):
            process_session_cards(
                self.conn, self._run_id(model), self.sid, model, room="den"
            )
        self.conn.rollback()

        self.assertEqual(model.calls, 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 0)
        rows = self.conn.execute(
            "SELECT step, raw_output FROM model_calls ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["step"] for r in rows], [
            "gen_cards_attempt", "gen_cards_attempt", "gen_cards_attempt_error"
        ])
        self.assertIn("turns:1-7", rows[0]["raw_output"])
        self.assertIn("third window failed", rows[2]["raw_output"])

    def test_auto_cards_propagates_session_failures(self):
        db.ingest_turns(self.conn, _messages(self.sid, 1, 5))
        model = FakeModel([RuntimeError("provider failed after accepting request")])

        with redirect_stdout(StringIO()), \
                self.assertRaisesRegex(RuntimeError, "failed for 1/1 sessions"):
            auto_generate_cards(self.conn, self._run_id(model), model)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM model_calls WHERE step='gen_cards_attempt_error'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
