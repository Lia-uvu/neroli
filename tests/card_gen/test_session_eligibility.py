"""First-card session eligibility, including explicit source-file exemptions."""
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
import pipeline  # noqa: E402
from memory_types import Message  # noqa: E402


class SessionEligibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _ingest(self, session_id: str, rounds: int, source_file: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        messages = [
            Message(
                role="user", speaker="user", text=f"round {rnd}", timestamp=ts,
                session_id=session_id, seq=rnd, round=rnd, label=str(rnd),
                source_uuid=f"{session_id}-{rnd}", source_file=source_file,
                message_seq=1, line_no=rnd,
            )
            for rnd in range(1, rounds + 1)
        ]
        db.ingest_turns(self.conn, messages)

    def test_phone_source_bypasses_first_session_turn_minimum(self) -> None:
        self._ingest("normal-short", 2, "/tmp/session.jsonl")
        self._ingest("normal-long", 3, "/tmp/long.jsonl")
        self._ingest("phone-short", 1, "/tmp/phone-20260719-1722-deadbeef.jsonl")
        settings = {
            "card_gen": {
                "min_first_session_turns": 3,
                "min_first_session_turns_exempt_source_globs": ["*/phone-*.jsonl"],
            }
        }

        with patch.object(pipeline, "load_settings", return_value=settings):
            session_ids = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}

        self.assertEqual(session_ids, {"normal-long", "phone-short"})

    def test_source_exemption_is_opt_in(self) -> None:
        self._ingest("phone-short", 1, "/tmp/phone-20260719-1722-deadbeef.jsonl")

        with patch.object(
            pipeline,
            "load_settings",
            return_value={"card_gen": {"min_first_session_turns": 3}},
        ):
            session_ids = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}

        self.assertNotIn("phone-short", session_ids)

    def test_source_policies_apply_distinct_first_card_thresholds(self) -> None:
        self._ingest("porch-short", 1, "/tmp/porch.jsonl")
        self.conn.execute(
            """
            INSERT INTO source_sessions
            (session_id, source, native_session_id, source_route, room)
            VALUES ('porch-short', 'porch', 'native-porch', 'den', 'den')
            """
        )
        self._ingest(
            "claude-short", 2,
            "/Users/test/.claude/projects/-tmp-room/session.jsonl",
        )
        self.conn.commit()
        settings = {
            "card_gen": {
                "min_first_session_turns": 3,
                "source_policies": {
                    "porch": {"enabled": True, "min_first_session_turns": 1},
                    "claude-code": {"enabled": True, "min_first_session_turns": 3},
                },
            }
        }
        with patch.object(pipeline, "load_settings", return_value=settings):
            selected = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}
        self.assertIn("porch-short", selected)
        self.assertNotIn("claude-short", selected)

    def test_source_round_gates_are_independent(self) -> None:
        self._ingest("porch-one", 1, "/tmp/porch.jsonl")
        self.conn.execute(
            """
            INSERT INTO source_sessions
            (session_id, source, native_session_id, source_route, room)
            VALUES ('porch-one', 'porch', 'native-one', 'den', 'den')
            """
        )
        self._ingest(
            "claude-four", 4,
            "/Users/test/.claude/projects/-tmp-room/session.jsonl",
        )
        self.conn.commit()
        settings = {
            "card_gen": {
                "source_policies": {
                    "porch": {"min_new_turns": 1, "min_interval_minutes": 10},
                    "claude-code": {"min_new_turns": 5, "min_interval_minutes": 60},
                }
            }
        }
        with patch.object(pipeline, "load_settings", return_value=settings):
            self.assertTrue(pipeline.check_card_gen_threshold(self.conn, "porch")[0])
            self.assertFalse(pipeline.check_card_gen_threshold(self.conn, "claude-code")[0])

    def test_phone_journal_is_distinct_from_claude_code_source(self) -> None:
        self._ingest(
            "phone-session", 3,
            "/Users/test/.claude/projects/-tmp-room/phone-20260810-abcd.jsonl",
        )
        self._ingest(
            "claude-session", 3,
            "/Users/test/.claude/projects/-tmp-room/session.jsonl",
        )
        self.assertEqual(db.source_for_session(self.conn, "phone-session"), "phone")
        self.assertEqual(
            db.source_for_session(self.conn, "claude-session"), "claude-code"
        )



if __name__ == "__main__":
    unittest.main()
