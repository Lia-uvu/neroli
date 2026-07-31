"""夜间 curator 提交门（submitcheck + ./submit 导出 + run_curation 复验）的回归。

守：字数/字段/constant_id 校验的判定口径、export_workbench 落 submit 并清陈旧
submission、run_curation 优先吃通过复验的 submission.json、复验不过退回 stdout 旧路径。
不调真实模型。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import curator  # noqa: E402
import submitcheck  # noqa: E402

GOOD = {
    "digest": "Amy 在收拾 recall-pipeline 准备开源。",
    "constants": [{"op": "add", "content": "Amy prefers ISO dates.", "shared": True,
                   "source_card_id": "ab#1"}],
}


class CheckTest(unittest.TestCase):
    def test_good_submission_passes(self):
        self.assertEqual(submitcheck.check(GOOD, set()), [])

    def test_digest_over_limit_reports_excess(self):
        # CJK 按 1 字 1 token 估算；默认上限 1000、校验宽限 10% → 1100
        errors = submitcheck.check({"digest": "这" * 1150, "constants": []}, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("1150 token", errors[0])
        self.assertIn("超出 50", errors[0])

    def test_token_estimate_mixes_cjk_and_ascii(self):
        # 4 个 CJK + 8 个 ascii → 4 + ceil(8/4) = 6
        self.assertEqual(submitcheck.estimate_tokens("中文四字abcdefgh"), 6)

    def test_digest_missing_or_blank(self):
        for data in ({"constants": []}, {"digest": "  ", "constants": []}):
            self.assertTrue(any("digest" in e for e in submitcheck.check(data, set())))

    def test_unknown_top_level_key(self):
        errors = submitcheck.check(dict(GOOD, constant=[]), set())
        self.assertTrue(any("constant" in e and "不认识" in e for e in errors))

    def test_update_retire_need_known_id(self):
        data = {"digest": "d", "constants": [
            {"op": "update", "constant_id": "k_ghost", "content": "x"},
            {"op": "retire", "constant_id": "k_real"},
        ]}
        errors = submitcheck.check(data, {"k_real"})
        self.assertEqual(len(errors), 1)
        self.assertIn("k_ghost", errors[0])
        # known_ids=None（wrapper 读不到 constants.json 时）跳过存在性检查
        self.assertEqual(submitcheck.check(data, None), [])

    def test_op_shape_errors(self):
        data = {"digest": "d", "constants": [
            {"op": "add"},                       # 缺 content
            {"op": "update", "constant_id": "k_a"},  # content/shared 全缺
            {"op": "drop", "constant_id": "k_a"},    # 不认识的 op
            "not-a-dict",
        ]}
        errors = submitcheck.check(data, {"k_a"})
        self.assertEqual(len(errors), 4)

    def test_constants_md_blank_rejected(self):
        errors = submitcheck.check(dict(GOOD, constants_md="  "), set())
        self.assertTrue(any("constants_md" in e for e in errors))

    def test_not_a_dict(self):
        self.assertTrue(submitcheck.check([GOOD], set()))


def _tmp_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return db.connect(Path(tmp.name)), Path(tmp.name)


class WorkbenchExportTest(unittest.TestCase):
    def test_submit_exported_and_stale_submission_cleared(self):
        conn, path = _tmp_conn()
        curator_dir = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(curator, "CURATOR_DIR", curator_dir):
                dest = curator.export_workbench(conn, "room", "2026-07-06", db_path=path)
                submit = dest / "submit"
                self.assertTrue(submit.exists())
                self.assertTrue(submit.stat().st_mode & 0o100)  # 可执行
                # 同夜重跑：上一轮的 submission.json 要被清掉
                (dest / "submission.json").write_text("{}", encoding="utf-8")
                curator.export_workbench(conn, "room", "2026-07-06", db_path=path)
                self.assertFalse((dest / "submission.json").exists())
        finally:
            conn.close()
            path.unlink(missing_ok=True)


class RunCurationSubmissionTest(unittest.TestCase):
    """run_curation 的取结果优先级：合法 submission.json > stdout 解析。"""

    def _run(self, submission: dict | str | None, raw: str = "跑完了，没别的输出。"):
        conn, path = _tmp_conn()
        # 给保险拴喂一张当晚可见的新卡，否则房间会被整个跳过
        conn.execute(
            "INSERT INTO cards (card_id, session_id, share, timestamp, room) "
            "VALUES ('t#1', 's1', '有新动静。', '2026-07-06T12:00:00+00:00', 'room')")
        rooms = Path(tempfile.mkdtemp())
        wb = Path(tempfile.mkdtemp())
        persona = wb / "agent-persona-room.md"
        persona.write_text("I am Claude, Amy's long-term coding collaborator.\n", encoding="utf-8")
        (wb / "constants.json").write_text("[]\n", encoding="utf-8")
        (wb / "tree-report.md").write_text("今晚有新卡的线：\n（无）\n", encoding="utf-8")

        class FakeModel:
            name = "fake"

            def run(self, prompt):
                if submission is not None:
                    text = submission if isinstance(submission, str) else \
                        json.dumps(submission, ensure_ascii=False)
                    (wb / "submission.json").write_text(text, encoding="utf-8")
                return raw

        with mock.patch.dict(curator.ROOM_DIRS,
                             {"room": rooms / "room", "other-room": rooms / "other-room"}, clear=False), \
             mock.patch("curator.curate_rooms", return_value=["room"]), \
             mock.patch("curator._agent_persona_file", return_value=persona), \
             mock.patch("curator.export_workbench", return_value=wb), \
             mock.patch("curator.digest_max_tokens", return_value=800), \
             mock.patch("curator.cleanup_old_workbenches", return_value=[]):
            try:
                res = curator.run_curation(conn, lambda cwd: FakeModel(), night="2026-07-06")
                digest_row = conn.execute("SELECT body FROM digests WHERE room='room'").fetchone()
                return res[0], digest_row["body"] if digest_row else None
            finally:
                conn.close()
                path.unlink(missing_ok=True)

    def test_valid_submission_wins(self):
        r, body = self._run(GOOD, raw='{"digest": "stdout 里的旧路径", "constants": []}')
        self.assertTrue(r["submitted"])
        self.assertEqual(body, GOOD["digest"])
        self.assertEqual(r["constants"]["add"], 1)

    def test_invalid_submission_falls_back_to_stdout(self):
        r, body = self._run({"digest": "这" * 1200, "constants": []},
                            raw='{"digest": "stdout 兜底", "constants": []}')
        self.assertFalse(r["submitted"])
        self.assertEqual(body, "stdout 兜底")

    def test_no_submission_keeps_old_path(self):
        r, body = self._run(None, raw='{"digest": "老流程照常", "constants": []}')
        self.assertFalse(r["submitted"])
        self.assertEqual(body, "老流程照常")

    def test_no_valid_submission_is_a_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, "no valid submission"):
            self._run(None, raw="done, but no JSON")


class SubmitWrapperTest(unittest.TestCase):
    """真跑一次导出的 ./submit 脚本（子进程），守 PASS/REJECTED 的行为。"""

    def _workbench(self):
        conn, path = _tmp_conn()
        curator_dir = Path(tempfile.mkdtemp())
        # digest 上限（token）烤进导出的 ./submit 脚本里；钉死 800，别让测试跟着部署 settings 漂
        with mock.patch.object(curator, "CURATOR_DIR", curator_dir), \
             mock.patch("curator.digest_max_tokens", return_value=800):
            dest = curator.export_workbench(conn, "room", "2026-07-06", db_path=path)
        conn.close()
        path.unlink(missing_ok=True)
        return dest

    def test_pass_writes_submission_and_reject_does_not(self):
        import subprocess
        dest = self._workbench()
        good = dest / "result.json"
        good.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
        out = subprocess.run([sys.executable, str(dest / "submit"), str(good)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("PASS", out.stdout)
        self.assertTrue((dest / "submission.json").exists())

        (dest / "submission.json").unlink()
        bad = dest / "bad.json"
        bad.write_text(json.dumps({"digest": "这" * 1000, "constants": []}, ensure_ascii=False), encoding="utf-8")
        out = subprocess.run([sys.executable, str(dest / "submit"), str(bad)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("REJECTED", out.stdout)
        self.assertIn("超出 120", out.stdout)  # 1000 token，宽限线 880
        self.assertFalse((dest / "submission.json").exists())

    def test_reject_not_json(self):
        import subprocess
        dest = self._workbench()
        out = subprocess.run([sys.executable, str(dest / "submit"), "-"],
                             input="这不是 json", capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("REJECTED", out.stdout)


if __name__ == "__main__":
    unittest.main()
