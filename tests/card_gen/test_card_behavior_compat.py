"""Behavioral compatibility probes shared with the pre-tree Card snapshot."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import gen_cards  # noqa: E402
import pipeline  # noqa: E402
from memory_types import Message  # noqa: E402


TIMESTAMP = "2026-08-03T12:00:00Z"


def messages(
    session_id: str,
    rounds: range,
    *,
    shared_prefix: int = 0,
) -> list[Message]:
    rows = []
    for round_no in rounds:
        source_uuid = (
            f"shared-{round_no}"
            if round_no <= shared_prefix
            else f"{session_id}-{round_no}"
        )
        rows.append(
            Message(
                role="user",
                speaker="user",
                text=f"round {round_no}",
                timestamp=TIMESTAMP,
                session_id=session_id,
                seq=round_no,
                round=round_no,
                label=str(round_no),
                source_uuid=source_uuid,
                message_seq=1,
                line_no=round_no,
            )
        )
    return rows


class FakeModel:
    name = "fake:compat"

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs[len(self.prompts) - 1]


class CardBehaviorCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "compat.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_card_creation_preserves_raw_turns_and_stores_card_layers(self) -> None:
        session_id = "compat00-session"
        db.ingest_turns(self.conn, messages(session_id, range(1, 4)))
        before = (
            self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
        )
        raw = (
            "turns:1-3\nheadline:event\nshare:shared fact\n"
            "private:room fact\ntags:alpha/beta"
        )
        model = FakeModel([raw])
        run_id = db.create_pipeline_run(self.conn, model.name, None, [])

        gen_cards.process_session_cards(
            self.conn, run_id, session_id, model, room="den"
        )

        after = (
            self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
        )
        self.assertEqual(after, before)
        card = self.conn.execute("SELECT * FROM cards").fetchone()
        self.assertEqual(
            (
                card["headline"], card["share"], card["private"],
                card["timestamp"], card["room"], card["model"],
            ),
            ("event", "shared fact", "room fact", TIMESTAMP, "den", model.name),
        )
        self.assertEqual(
            [row["tag"] for row in self.conn.execute(
                "SELECT tag FROM card_tags ORDER BY tag"
            )],
            ["alpha", "beta"],
        )
        self.assertEqual(
            self.conn.execute("SELECT raw FROM card_raw").fetchone()[0], raw
        )

    def test_global_round_and_attempt_cooldown_gates_stay_separate(self) -> None:
        session_id = "global00-session"
        settings = {"card_gen": {"min_new_turns": 5, "min_interval_minutes": 60}}
        db.ingest_turns(self.conn, messages(session_id, range(1, 5)))
        with patch.object(pipeline, "load_settings", return_value=settings):
            allowed, reason = pipeline.check_card_gen_threshold(self.conn)
        self.assertFalse(allowed)
        self.assertIn("only 4 new rounds", reason)

        db.ingest_turns(self.conn, messages(session_id, range(5, 6)))
        with patch.object(pipeline, "load_settings", return_value=settings):
            allowed, reason = pipeline.check_card_gen_threshold(self.conn)
        self.assertTrue(allowed, reason)

        run_id = db.create_pipeline_run(self.conn, "fake:compat", None, [])
        db.record_model_call(
            self.conn, run_id, "gen_cards_attempt", "prompt", "raw", {},
            session_id=session_id,
        )
        self.conn.commit()
        with patch.object(pipeline, "load_settings", return_value=settings):
            allowed, reason = pipeline.check_card_gen_threshold(self.conn)
        self.assertFalse(allowed)
        self.assertIn("too soon", reason)

    def test_fork_first_card_eligibility_counts_only_delta_rounds(self) -> None:
        parent = "parent00-session"
        child = "child000-session"

        def force_fork(child_turns: int) -> None:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO session_forks
                (child_session_id, parent_session_id, fork_round,
                 parent_fork_round, delta_start_round, shared_turns,
                 child_turns, parent_turns, child_shared_ratio)
                VALUES (?, ?, 5, 5, 6, 5, ?, 5, ?)
                """,
                (child, parent, child_turns, 5 / child_turns),
            )
            self.conn.commit()

        db.ingest_turns(self.conn, messages(parent, range(1, 6), shared_prefix=5))
        db.ingest_turns(self.conn, messages(child, range(1, 7), shared_prefix=5))
        force_fork(6)
        settings = {"card_gen": {"min_first_session_turns": 3}}

        with patch.object(pipeline, "load_settings", return_value=settings):
            selected = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}
        self.assertNotIn(child, selected)

        db.ingest_turns(self.conn, messages(child, range(7, 9), shared_prefix=5))
        force_fork(8)
        with patch.object(pipeline, "load_settings", return_value=settings):
            selected = {sid for sid, _room in pipeline.sessions_needing_update(self.conn)}
        self.assertIn(child, selected)

    def test_one_card_bootstrap_keeps_the_complete_opening(self) -> None:
        model = FakeModel([
            "turns:1-2\nheadline:first\nshare:first\nprivate:\ntags:first",
            "turns:1-4\nheadline:whole\nshare:whole\nprivate:\ntags:whole",
        ])
        with patch.object(
            gen_cards,
            "_prompt_template",
            return_value="{existing_summary}\n---\n{conversation}",
        ):
            gen_cards.generate(
                messages("bootstrap-session", range(1, 5)),
                model,
                init_rounds=2,
                grow=2,
            )

        self.assertEqual(len(model.prompts), 2)
        self.assertIn("round 1", model.prompts[1])
        self.assertIn("round 4", model.prompts[1])

    def test_two_cards_freeze_prefix_and_refeed_from_inclusive_boundary(self) -> None:
        model = FakeModel([
            (
                "turns:1-2\nheadline:a\nshare:a\nprivate:\ntags:a\n\n"
                "turns:2-4\nheadline:b\nshare:b\nprivate:\ntags:b"
            ),
            "turns:2-6\nheadline:c\nshare:c\nprivate:\ntags:c",
        ])
        with patch.object(
            gen_cards,
            "_prompt_template",
            return_value="{existing_summary}\n---\n{conversation}",
        ):
            cards = gen_cards.generate(
                messages("freeze00-session", range(1, 7)),
                model,
                init_rounds=4,
                grow=2,
            )

        self.assertEqual(len(model.prompts), 2)
        self.assertNotIn("round 1\n", model.prompts[1])
        self.assertIn("round 2", model.prompts[1])
        self.assertIn("round 6", model.prompts[1])
        self.assertEqual([card["headline"] for card in cards], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
