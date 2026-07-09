"""夜间 curator 阶段3 的落盘逻辑回归（不调模型）。

守：输出解析、constants 增量指令落表、digest/constants.md 渲染。
用假的 model_factory 跑通 run_curation 的解析→落盘链路，ROOM_DIRS 打到临时目录，
绝不写真实 agent room，也绝不碰 data/fragments.db。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import curator  # noqa: E402

FENCED = """好的，我看完了。

```json
{
  "digest": "要点一\\n要点二",
  "constants": [
    {"op": "add", "content": "Amy prefers ISO dates.", "shared": true, "source_card_id": "ab#1"},
    {"op": "add", "content": "Claude keeps deployment notes private.", "shared": false}
  ]
}
```
"""


def _tmp_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return db.connect(Path(tmp.name)), Path(tmp.name)


class ParseTest(unittest.TestCase):
    def test_fenced_json(self):
        p = curator._parse_output(FENCED)
        self.assertEqual(p["digest"], "要点一\n要点二")
        self.assertEqual(len(p["constants"]), 2)

    def test_bare_braces(self):
        p = curator._parse_output('prefix {"digest": "x", "constants": []} suffix')
        self.assertEqual(p["digest"], "x")

    def test_garbage_is_empty(self):
        self.assertEqual(curator._parse_output("no json here"), {})

    def test_digest_limit_prefers_paragraphs(self):
        text = "第一段" + "x" * 20 + "\n\n" + "第二段" + "y" * 20 + "\n\n" + "第三段" + "z" * 20
        limited = curator._limit_digest(text, max_chars=60)
        self.assertIn("第一段", limited)
        self.assertIn("第二段", limited)
        self.assertNotIn("第三段", limited)
        self.assertLessEqual(len(limited), 60)

    def test_recall_wrapper_has_jieba_fallback(self):
        self.assertIn('types.ModuleType("jieba")', curator._RECALL_TEMPLATE)
        self.assertIn('sys.modules["jieba"]', curator._RECALL_TEMPLATE)


class WritebackTest(unittest.TestCase):
    def test_ops_and_render(self):
        conn, path = _tmp_conn()
        rooms = Path(tempfile.mkdtemp())
        try:
            with mock.patch.dict(curator.ROOM_DIRS,
                                 {"room": rooms / "room", "other-room": rooms / "other-room"}, clear=False):
                p = curator._parse_output(FENCED)
                curator._write_digest(conn, "room", "2026-07-02", p["digest"], "cli:codex")
                a = curator._apply_constants_ops(conn, "room", p["constants"])
                self.assertEqual(a["add"], 2)

                shared_id = conn.execute(
                    "SELECT constant_id FROM constants WHERE shared=1"
                ).fetchone()["constant_id"]
                u = curator._apply_constants_ops(
                    conn, "room", [{"op": "update", "constant_id": shared_id, "content": "Amy prefers YYYY-MM-DD dates."}])
                self.assertEqual(u["update"], 1)
                r = curator._apply_constants_ops(
                    conn, "room", [{"op": "retire", "constant_id": "nope"}])
                self.assertEqual(r["retire"], 0)  # 不存在的 id 不生效
                self.assertEqual(r["skipped"], 1)

                curator.render_constants_md(conn, "room")
                conn.commit()

                digest_md = (rooms / "room" / "digest.md").read_text(encoding="utf-8")
                self.assertIn("要点一", digest_md)
                self.assertEqual(
                    conn.execute("SELECT body FROM digests WHERE room='room'").fetchone()["body"],
                    "要点一\n要点二")

                constants_md = (rooms / "room" / "constants.md").read_text(encoding="utf-8")
                self.assertIn("Amy prefers YYYY-MM-DD dates.", constants_md)   # 更新后的内容
                self.assertIn("Claude keeps deployment notes private.", constants_md)
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_shared_constant_crosses_rooms(self):
        # shared=1 的 constant 在 constants.md 的「全院共享」段对任何房间都出现。
        conn, path = _tmp_conn()
        rooms = Path(tempfile.mkdtemp())
        try:
            with mock.patch.dict(curator.ROOM_DIRS,
                                 {"room": rooms / "room", "other-room": rooms / "other-room"}, clear=False):
                curator._apply_constants_ops(
                    conn, "room", [{"op": "add", "content": "Shared fact visible to all rooms.", "shared": True}])
                conn.commit()
                curator.render_constants_md(conn, "other-room")
                self.assertIn("Shared fact visible to all rooms.",
                              (rooms / "other-room" / "constants.md").read_text(encoding="utf-8"))
        finally:
            conn.close()
            path.unlink(missing_ok=True)


class RunCurationRenderTest(unittest.TestCase):
    """run_curation 的 constants.md 渲染决策（不调真实模型，export_workbench 打桩）。"""

    def _run(self, raw: str, night="2026-07-02"):
        conn, path = _tmp_conn()
        # 给保险拴喂一张当晚可见的新卡，否则房间会被整个跳过
        conn.execute(
            "INSERT INTO cards (card_id, session_id, share, timestamp, room) "
            "VALUES ('t#1', 's1', 'Amy chose the repository license.', ?, 'room')",
            (f"{night}T12:00:00+00:00",))
        rooms = Path(tempfile.mkdtemp())
        wb = Path(tempfile.mkdtemp())
        persona = wb / "agent-persona-room.md"
        persona.write_text("I am Claude, Amy's long-term coding collaborator.\n", encoding="utf-8")
        (wb / "constants.json").write_text("[]\n", encoding="utf-8")
        (wb / "prev-digest.md").write_text("Amy is maintaining recall-pipeline.\n", encoding="utf-8")
        (wb / "tree-report.md").write_text(
            "== 树变化 2026-07-02（对比 2026-07-01）==\n\n"
            "顶层社区（卡数 / 24h新卡 / 7d新卡）：\n"
            "  c0001 recall-pipeline(4) 开源(2)   6卡  +1/24h  +3/7d\n\n"
            "今晚有新卡的线（新=上次快照以来，按新卡数排序）：\n"
            "  c0001 [recall-pipeline(4) 开源(2)]  +1卡（共6卡）\n"
            "        新卡样例：Amy chose the repository license.\n",
            encoding="utf-8",
        )
        prompts = []

        class FakeModel:
            name = "fake"

            def run(self, prompt):
                prompts.append(prompt)
                return raw

        with mock.patch.dict(curator.ROOM_DIRS,
                             {"room": rooms / "room", "other-room": rooms / "other-room"}, clear=False), \
             mock.patch("curator.curate_rooms", return_value=["room"]), \
             mock.patch("curator._agent_persona_file", return_value=persona), \
             mock.patch("curator.export_workbench", return_value=wb), \
             mock.patch("curator.cleanup_old_workbenches", return_value=[]):
            res = curator.run_curation(conn, lambda cwd: FakeModel(), night=night)
        conn.close()
        path.unlink(missing_ok=True)
        return res, rooms / "room", prompts

    def test_constants_md_wins_over_mechanical(self):
        raw = ('```json\n{"digest": "要点A（07-02）",'
               ' "constants": [{"op": "add", "content": "事实X", "shared": true}],'
               ' "constants_md": "# 组织版\\n- 事实X"}\n```')
        res, room_dir, _ = self._run(raw)
        self.assertIn("要点A", (room_dir / "digest.md").read_text(encoding="utf-8"))
        self.assertEqual((room_dir / "constants.md").read_text(encoding="utf-8").strip(),
                         "# 组织版\n- 事实X")  # 模型组织版，非机械渲染
        self.assertEqual(res[0]["constants"]["add"], 1)
        self.assertTrue(res[0]["constants_md"])

    def test_no_change_night_skips_constants_md(self):
        raw = '```json\n{"digest": "只更新 digest（07-02）", "constants": []}\n```'
        res, room_dir, _ = self._run(raw)
        self.assertIn("只更新", (room_dir / "digest.md").read_text(encoding="utf-8"))
        self.assertFalse((room_dir / "constants.md").exists())  # 无变动、无组织版 → 不重写

    def test_change_without_constants_md_falls_back(self):
        raw = ('```json\n{"digest": "d（07-02）",'
               ' "constants": [{"op": "add", "content": "机械兜底的事实", "shared": false}]}\n```')
        res, room_dir, _ = self._run(raw)
        # 有变动但没给 constants_md → 机械 render 兜底（带标准页眉）
        text = (room_dir / "constants.md").read_text(encoding="utf-8")
        self.assertIn("永久有效的事实篮子", text)
        self.assertIn("机械兜底的事实", text)

    def test_no_fresh_cards_skips_room(self):
        # 保险拴：没有新卡的房间当晚整个跳过——不导出工作台、不调模型
        conn, path = _tmp_conn()
        try:
            with mock.patch("curator.curate_rooms", return_value=["room"]), \
                 mock.patch("curator.export_workbench",
                            side_effect=AssertionError("不该导出工作台")), \
                 mock.patch("curator.cleanup_old_workbenches", return_value=[]):
                res = curator.run_curation(
                    conn, lambda cwd: self.fail("不该调模型"), night="2026-07-02")
            self.assertEqual(res, [{"room": "room", "skipped": "no-fresh-cards"}])
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_room_persona_is_injected(self):
        raw = '```json\n{"digest": "d", "constants": []}\n```'
        _res, _den, prompts = self._run(raw)
        self.assertEqual(len(prompts), 1)
        self.assertIn("这是你的AGENTS.md:", prompts[0])
        self.assertIn("I am Claude, Amy's long-term coding collaborator.", prompts[0])
        self.assertIn("现在你需要维护更新自己的记忆", prompts[0])
        self.assertIn("Amy is maintaining recall-pipeline.", prompts[0])
        self.assertIn("今晚有新卡的线", prompts[0])
        self.assertIn("顶层社区（卡数 / 24h新卡 / 7d新卡）", prompts[0])
        self.assertNotIn("{agent-persona}", prompts[0])
        self.assertNotIn("{pre-digest}", prompts[0])
        self.assertNotIn("{tree-diff}", prompts[0])


if __name__ == "__main__":
    unittest.main()
