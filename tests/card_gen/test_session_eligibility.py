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


if __name__ == "__main__":
    unittest.main()
