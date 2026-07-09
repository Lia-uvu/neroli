"""中期层 treesnap 的正确性回归。

守四件事：
  1. diff 的叶子层身份匹配能把 new / continued / merged / split 四类事件各自分对。
  2. render_report 新卡驱动：老叶被重聚类打散、碎片全是旧卡时，报告事件区不得出现这些旧卡，
     只有含 fresh 卡的线才上报（回归「妈去医院被当新叶摘出」）。
  3. snapshot 把当前 clusters/cluster_members 原样拷成一夜，并按 keep_nights 清过期夜。
  4. db.card_visible_clause（可见性谓词下沉后）与历史内联谓词逐字等价，且实际过滤正确。

临时库、绝不碰 data/fragments.db。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import treesnap  # noqa: E402


def _tmp_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return db.connect(Path(tmp.name)), Path(tmp.name)


def _put_snapshot(conn, night: str, leaves: dict[str, list[str]]):
    """把 {leaf_cluster_id: [card_id...]} 直接写成一夜快照（全是无子叶子）。"""
    for cid, cards in leaves.items():
        conn.execute(
            "INSERT INTO tree_snapshots (night, cluster_id, parent_cluster_id, level, summary) VALUES (?, ?, NULL, 1, '')",
            (night, cid),
        )
        for card_id in cards:
            conn.execute(
                "INSERT INTO tree_snapshot_members (night, cluster_id, card_id, role) VALUES (?, ?, ?, 'primary')",
                (night, cid, card_id),
            )
    conn.commit()


class DiffEventsTest(unittest.TestCase):
    def test_four_event_types(self):
        conn, path = _tmp_conn()
        try:
            # 昨夜 n1
            _put_snapshot(conn, "n1", {
                "O_cont":   ["c1", "c2", "c3"],
                "O_merge1": ["c10", "c11"],
                "O_merge2": ["c12", "c13"],
                "O_split":  ["c20", "c21", "c22", "c23"],
            })
            # 今夜 n2
            _put_snapshot(conn, "n2", {
                "N_cont":   ["c1", "c2", "c3", "c4"],          # O_cont + c4
                "N_merged": ["c10", "c11", "c12", "c13"],      # O_merge1 + O_merge2
                "N_a":      ["c20", "c21"],                    # O_split 一半
                "N_b":      ["c22", "c23"],                    # O_split 另一半
                "N_new":    ["c30", "c31"],                    # 全新，无 O 落入
            })
            ev = treesnap.diff(conn, "n2", "n1")

            self.assertTrue(any(e["cluster_id"] == "N_new" for e in ev["new"]),
                            f"new 未识别: {ev['new']}")
            cont = [e for e in ev["continued"] if e["from"] == "O_cont"]
            self.assertEqual(len(cont), 1)
            self.assertEqual(cont[0]["cluster_id"], "N_cont")
            self.assertEqual(cont[0]["added"], ["c4"])
            merged = [e for e in ev["merged"] if e["cluster_id"] == "N_merged"]
            self.assertEqual(len(merged), 1)
            self.assertEqual(set(merged[0]["from"]), {"O_merge1", "O_merge2"})
            split = [e for e in ev["split"] if e["cluster_id"] == "O_split"]
            self.assertEqual(len(split), 1)
            self.assertEqual(set(split[0]["into"]), {"N_a", "N_b"})
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_dissolved_stale_leaf_absent_from_report(self):
        # 老叶 O（全是一周前的卡）被重聚类打散成 X/Y，另有一张新卡自成叶 Z。
        # 新卡驱动的报告：只有 Z 上事件区，X/Y 的旧卡 theme 不得出现，打散记一处「拓扑重排」。
        conn, path = _tmp_conn()
        try:
            old_ts, fresh_ts = "2026-06-24T12:00:00+00:00", "2026-07-03T09:00:00+00:00"
            for cid, ts, theme in [
                ("o1", old_ts, "妈第一次去医院"),
                ("o2", old_ts, "妈复诊肾科"),
                ("o3", old_ts, "陪护排班"),
                ("o4", old_ts, "医保报销流程"),
                ("f1", fresh_ts, "FF14 7.4 上线笔记"),
            ]:
                conn.execute(
                    "INSERT INTO cards (card_id, session_id, theme, timestamp, room) VALUES (?, 's', ?, ?, 'room')",
                    (cid, theme, ts),
                )
            _put_snapshot(conn, "2026-06-25", {"O": ["o1", "o2", "o3", "o4"]})
            _put_snapshot(conn, "2026-07-03", {
                "X": ["o1", "o2"],   # O 的一半，全旧卡
                "Y": ["o3", "o4"],   # O 的另一半，全旧卡
                "Z": ["f1"],         # 今晚唯一的新卡，自成一叶
            })
            report = treesnap.render_report(conn, night="2026-07-03")

            self.assertNotIn("医院", report)      # 旧卡 theme 被 fresh 闸挡掉
            self.assertNotIn("肾科", report)
            self.assertIn("FF14 7.4 上线笔记", report)  # 唯一有新卡的线，样例是新卡
            self.assertIn("+1卡", report)
            self.assertIn("拓扑重排 1 处", report)      # O 打散成 X/Y、去向全无新卡
        finally:
            conn.close()
            path.unlink(missing_ok=True)


class SnapshotPruneTest(unittest.TestCase):
    def test_snapshot_copies_and_prunes(self):
        conn, path = _tmp_conn()
        try:
            conn.execute("INSERT INTO clusters (cluster_id, summary, parent_cluster_id, level) VALUES ('c1','x',NULL,1)")
            conn.execute("INSERT INTO clusters (cluster_id, summary, parent_cluster_id, level) VALUES ('c2','y','c1',2)")
            for card_id in ("k1", "k2", "k3"):
                conn.execute(
                    "INSERT INTO cards (card_id, session_id, theme, room) VALUES (?, 's', 't', 'room')",
                    (card_id,),
                )
                conn.execute("INSERT INTO cluster_members (card_id, cluster_id, role) VALUES (?, 'c2', 'primary')", (card_id,))
            conn.commit()

            s1 = treesnap.snapshot(conn, night="2026-01-01", keep_nights=2)
            self.assertEqual(s1["clusters"], 2)
            self.assertEqual(s1["primary_members"], 3)
            treesnap.snapshot(conn, night="2026-01-02", keep_nights=2)
            treesnap.snapshot(conn, night="2026-01-03", keep_nights=2)

            nights = treesnap.snapshot_nights(conn)
            self.assertEqual(nights, ["2026-01-03", "2026-01-02"])  # 01-01 被 keep_nights=2 清掉
            self.assertEqual(treesnap.prev_night(conn, "2026-01-03"), "2026-01-02")
        finally:
            conn.close()
            path.unlink(missing_ok=True)


class VisibleClauseTest(unittest.TestCase):
    def test_literal_parity(self):
        # 与历史内联谓词逐字等价（回归护栏）。
        self.assertEqual(
            db.card_visible_clause("room", "c"),
            ("(c.room = ? OR NULLIF(TRIM(COALESCE(c.share, '')), '') IS NOT NULL)", ["room"]),
        )
        self.assertEqual(
            db.card_visible_clause("other-room", "cards"),
            ("(cards.room = ? OR NULLIF(TRIM(COALESCE(cards.share, '')), '') IS NOT NULL)", ["other-room"]),
        )
        self.assertEqual(db.card_visible_clause(None, "c"), ("1=1", []))

    def test_functional_filter(self):
        conn, path = _tmp_conn()
        try:
            rows = [
                ("room_own", "room", ""),          # 本房，无 share
                ("room_shared", "room", "s"),      # 本房，有 share
                ("other-room_own", "other-room", ""),          # 他房，无 share → room 不可见
                ("other-room_shared", "other-room", "s"),      # 他房，有 share → room 可见
            ]
            for cid, room, share in rows:
                conn.execute(
                    "INSERT INTO cards (card_id, session_id, theme, share, room) VALUES (?, 's', 't', ?, ?)",
                    (cid, share, room),
                )
            conn.commit()
            clause, params = db.card_visible_clause("room", "c")
            visible = {
                r["card_id"] for r in conn.execute(
                    f"SELECT card_id FROM cards c WHERE {clause}", params
                ).fetchall()
            }
            self.assertEqual(visible, {"room_own", "room_shared", "other-room_shared"})
        finally:
            conn.close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
