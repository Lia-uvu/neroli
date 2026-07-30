"""Last-24 cards rename and verified summary workbench."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import context  # noqa: E402
import db  # noqa: E402
import last24  # noqa: E402
import summarycheck  # noqa: E402


class SummaryCheckTest(unittest.TestCase):
    def test_enforces_exact_character_limit(self):
        self.assertEqual(summarycheck.check({"summary": "这" * 700}), [])
        errors = summarycheck.check({"summary": "这" * 701})
        self.assertEqual(len(errors), 1)
        self.assertIn("超出 1 字", errors[0])

    def test_only_accepts_nonempty_summary(self):
        self.assertTrue(summarycheck.check({"summary": ""}))
        self.assertTrue(summarycheck.check({"summary": "ok", "extra": 1}))


class Last24RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "test.db"
        self.conn = db.connect(self.db_path)
        self.room_dir = self.root / "room"
        self.workbenches = self.root / "workbenches"
        self.conn.execute(
            "INSERT INTO cards "
            "(card_id, session_id, headline, share, private, timestamp, room) "
            "VALUES ('a#1', 's1', 'Amy finished the migration', 'migration done', '', "
            "datetime('now'), 'room')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _patches(self):
        return (
            mock.patch.dict(context.ROOM_DIRS, {"room": self.room_dir}, clear=True),
            mock.patch.object(context, "ROOMS", ("room",)),
            mock.patch.dict(last24.ROOM_DIRS, {"room": self.room_dir}, clear=True),
            mock.patch.object(last24, "ROOMS", ("room",)),
            mock.patch.object(last24, "WORKBENCH_DIR", self.workbenches),
            mock.patch.object(last24, "summary_settings", return_value={
                "enabled": True, "agent_name": "Sol", "max_chars": 700,
            }),
        )

    def test_export_has_cards_recall_and_submit(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            context.rebuild_context(self.conn)
            dest = last24.export_workbench(self.conn, "room", self.db_path)
        self.assertTrue((self.room_dir / "cards-last-24.md").exists())
        self.assertIn("Amy finished", (dest / "cards-last-24.md").read_text())
        self.assertTrue((dest / "recall").stat().st_mode & 0o100)
        self.assertTrue((dest / "submit").stat().st_mode & 0o100)

    def test_prompt_injects_agent_timezone_and_character_budget(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             mock.patch.object(last24, "load_settings",
                               return_value={"timezone": "Asia/Shanghai"}):
            prompt = last24.fill_prompt("room")
        self.assertIn("你是 Sol", prompt)
        self.assertIn("Asia/Shanghai", prompt)
        self.assertIn("上限 `700` 字", prompt)
        self.assertNotIn("{summary-agent}", prompt)
        self.assertNotIn("{timezone}", prompt)
        self.assertNotIn("{now}", prompt)

    def test_valid_submission_is_rechecked_and_written(self):
        patches = self._patches()

        class FakeModel:
            name = "fake"

            def __init__(self, cwd):
                self.cwd = Path(cwd)

            def run(self, prompt):
                self.assert_prompt = prompt
                (self.cwd / "submission.json").write_text(
                    json.dumps({"summary": "Amy 已完成迁移，当前没有遗留步骤。"},
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                return "(success artifact)"

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            context.rebuild_context(self.conn)
            results = last24.run_summaries(
                self.conn, lambda cwd: FakeModel(cwd), db_path=self.db_path)
        self.assertEqual(results[0]["summary_chars"], 19)
        body = (self.room_dir / "summary-last-24.md").read_text(encoding="utf-8")
        self.assertIn("Sol 根据事件卡整理", body)
        self.assertIn("当前没有遗留步骤", body)

    def test_invalid_artifact_and_stdout_are_hard_failure(self):
        patches = self._patches()

        class FakeModel:
            name = "fake"

            def __init__(self, cwd):
                self.cwd = Path(cwd)

            def run(self, _prompt):
                (self.cwd / "submission.json").write_text(
                    json.dumps({"summary": "太" * 701}), encoding="utf-8")
                return "done"

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            context.rebuild_context(self.conn)
            with self.assertRaisesRegex(RuntimeError, "no valid submission"):
                last24.run_summaries(
                    self.conn, lambda cwd: FakeModel(cwd), db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
