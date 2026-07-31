"""retrieval 检索层的回归（2026-07-15 的匹配语义改造）。

守：多词查询 AND 优先/OR 补位、查询侧子词展开对齐索引分词、FTS 命中带
上下文片段（CardRef.why）、tag 命中标明来源、private 不参与跨房间匹配、
session_siblings 的顺序与可见性、wander 的冷卡优先。

语义检索（semantic_search）依赖 .emb_cache / embedding API，不在此覆盖——
它的隐私分路（跨房间只用 share 向量）靠 code review 和 skills/ops/search.md 守。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import retrieval  # noqa: E402


def _tmp_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return db.connect(Path(tmp.name)), Path(tmp.name)


def _card(card_id, headline="", share="", private="", room="roomA",
          ts="2026-07-01T12:00:00+00:00", session="s1", turns=(1, 2), tags=()):
    return {"card_id": card_id, "session_id": session, "turns": list(turns),
            "headline": headline, "share": share, "private": private,
            "timestamp": ts, "room": room, "tags": list(tags)}


class QueryExprTest(unittest.TestCase):
    def test_compound_word_expands_to_subwords(self):
        exprs, flat = db._jieba_query_exprs("生日礼物")
        self.assertEqual(len(exprs), 1)  # lcut 切成一个主词
        for tok in ('"生日礼物"', '"生日"', '"礼物"'):
            self.assertIn(tok, exprs[0])  # 整词或子词组合都算命中
        self.assertGreater(len(flat), 1)

    def test_punctuation_only_dropped(self):
        exprs, flat = db._jieba_query_exprs("，。！？")
        self.assertEqual(exprs, [])
        self.assertEqual(flat, [])


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _tmp_conn()
        db.insert_card(self.conn, _card(
            "a#1", headline="朋友的生日", share="我们聊了生日和礼物怎么挑", turns=(1, 2)))
        db.insert_card(self.conn, _card(
            "a#2", headline="下午的采购", share="挑了一个礼物", turns=(3, 4)))
        db.insert_card(self.conn, _card(
            "a#3", headline="失眠的一晚", share="翻来覆去睡不着", session="s2",
            tags=("爷爷生日",)))
        db.insert_card(self.conn, _card(
            "b#1", headline="别房的私事", private="秘密计划", share="", room="roomB", session="s3"))
        db.insert_card(self.conn, _card(
            "b#2", headline="别房的日常", share="有 share 的卡", private="梧桐树下的约定",
            room="roomB", session="s3", turns=(5, 6)))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.path.unlink(missing_ok=True)

    def test_and_hits_rank_before_or_fill(self):
        hits = retrieval.search(self.conn, "生日礼物", viewer="roomA")
        ids = [h.card_id for h in hits]
        self.assertEqual(ids[0], "a#1")  # 两个词都带的卡排最前
        self.assertIn("a#2", ids)        # 只带"礼物"的靠任一词命中补位

    def test_body_hit_carries_snippet_reason(self):
        hits = retrieval.search(self.conn, "礼物", viewer="roomA")
        h = next(x for x in hits if x.card_id == "a#1")
        self.assertIn("礼物", h.why)  # headline 没有"礼物"，片段来自 share

    def test_tag_hit_labeled_with_tag(self):
        hits = retrieval.search(self.conn, "生日礼物", viewer="roomA")
        why = {h.card_id: h.why for h in hits}
        self.assertIn("a#3", why)  # 经 "爷爷生日" tag 子串命中
        self.assertTrue(why["a#3"].startswith("tag:"), why["a#3"])
        self.assertIn("爷爷生日", why["a#3"])

    def test_private_not_matched_cross_room(self):
        # b#2 对 roomA 可见（有 share），但它的 private 不参与 roomA 的匹配
        self.assertNotIn("b#2", [h.card_id for h in
                                 retrieval.search(self.conn, "梧桐", viewer="roomA")])
        self.assertIn("b#2", [h.card_id for h in
                              retrieval.search(self.conn, "梧桐", viewer="roomB")])
        # 无 share 的他房卡整卡不可见
        self.assertNotIn("b#1", [h.card_id for h in
                                 retrieval.search(self.conn, "秘密", viewer="roomA")])


class SessionSiblingsTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _tmp_conn()
        db.insert_card(self.conn, _card("a#2", headline="后半场", turns=(3, 4)))
        db.insert_card(self.conn, _card("a#1", headline="前半场", turns=(1, 2)))
        db.insert_card(self.conn, _card("b#1", headline="他房无share", share="", room="roomB", session="s3", turns=(1, 2)))
        db.insert_card(self.conn, _card("b#2", headline="他房有share", share="s", room="roomB", session="s3", turns=(3, 4)))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.path.unlink(missing_ok=True)

    def test_turn_order_includes_self(self):
        sibs = retrieval.session_siblings(self.conn, "a#2", viewer="roomA")
        self.assertEqual([s.card_id for s in sibs], ["a#1", "a#2"])

    def test_cross_room_filters_shareless_siblings(self):
        sibs = retrieval.session_siblings(self.conn, "b#2", viewer="roomA")
        self.assertEqual([s.card_id for s in sibs], ["b#2"])  # b#1 无 share，不该露
        self.assertIsNone(retrieval.session_siblings(self.conn, "b#1", viewer="roomA"))


class WanderTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _tmp_conn()
        db.insert_card(self.conn, _card("old#1", headline="翻过的旧卡", ts="2026-01-01T10:00:00+00:00"))
        db.insert_card(self.conn, _card("old#2", headline="没翻过的旧卡", ts="2026-01-02T10:00:00+00:00"))
        db.insert_card(self.conn, _card("new#1", headline="昨天的新卡", ts="2126-01-01T10:00:00+00:00"))
        for _ in range(2):
            self.conn.execute(
                "INSERT INTO card_access (card_id, viewer, source, ts) VALUES ('old#1','roomA','search','2026-07-01T00:00:00+00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.path.unlink(missing_ok=True)

    def test_cold_first_and_recent_excluded(self):
        hits = retrieval.wander(self.conn, limit=10, viewer="roomA")
        ids = [h.card_id for h in hits]
        self.assertNotIn("new#1", ids)             # 七天内的不进漫游
        self.assertEqual(ids[0], "old#2")          # 零取用的排最前
        why = {h.card_id: h.why for h in hits}
        self.assertEqual(why["old#2"], "还没翻过")
        self.assertEqual(why["old#1"], "翻过 2 次")


if __name__ == "__main__":
    unittest.main()
