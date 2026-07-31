"""增量 ingest 的正确性回归。

重点守两件事：
  1. 一个 session 被 fork/续接劈到多个文件时，只要其中一个文件变化，增量必须把该 session
     的**全部**文件一起加载，round 不能被局部重编号写坏。
  2. fork 出的父/子是两个独立 session，各自独立编号、互不牵连；只有子文件变化时不该重读父文件。

用真实 JSONL 文件跑真实加载路径，临时库、临时目录，绝不碰 data/fragments.db。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db
from incremental import current_mtimes, select_incremental
from loaders import load_messages_for_ingest


def _line(session_id: str, uuid: str, role: str, text: str, ts: str, parent: str = "") -> str:
    return json.dumps({
        "sessionId": session_id,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "message": {"role": role, "content": text},
    }, ensure_ascii=False)


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rounds(conn, session_id: str):
    return [
        (r["round"], r["message_seq"])
        for r in conn.execute(
            "SELECT round, message_seq FROM turns WHERE session_id=? ORDER BY round, message_seq",
            (session_id,),
        ).fetchall()
    ]


def _ingest(conn, paths):
    return db.ingest_turns(conn, load_messages_for_ingest(paths))


class IncrementalIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_split_session_reloads_all_its_files(self) -> None:
        """S 的轮次劈在 a(1-2) 和 b(3-4)；只动 b，增量须连 a 一起读，S 仍是 1-4。"""
        S = "sess-split"
        a = self.tmp / "a.jsonl"
        b = self.tmp / "b.jsonl"
        _write(a, [
            _line(S, "u1", "user", "问1", "2026-06-01T00:00:01Z"),
            _line(S, "a1", "assistant", "答1", "2026-06-01T00:00:02Z"),
            _line(S, "u2", "user", "问2", "2026-06-01T00:00:03Z"),
            _line(S, "a2", "assistant", "答2", "2026-06-01T00:00:04Z"),
        ])
        _write(b, [
            _line(S, "u3", "user", "问3", "2026-06-02T00:00:01Z"),
            _line(S, "a3", "assistant", "答3", "2026-06-02T00:00:02Z"),
            _line(S, "u4", "user", "问4", "2026-06-02T00:00:03Z"),
            _line(S, "a4", "assistant", "答4", "2026-06-02T00:00:04Z"),
        ])
        _ingest(self.conn, [a, b])
        self.assertEqual(_rounds(self.conn, S), [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2), (4, 1), (4, 2)])

        state = current_mtimes([a, b])
        os.utime(b, (state[str(b)] + 10, state[str(b)] + 10))  # 只有 b 变了

        selected, _ = select_incremental(self.conn, [a, b], state)
        self.assertEqual({p.name for p in selected}, {"a.jsonl", "b.jsonl"})  # 连通分量拉进 a

        _ingest(self.conn, selected)
        self.assertEqual(_rounds(self.conn, S), [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2), (4, 1), (4, 2)])

    def test_naive_single_file_would_corrupt(self) -> None:
        """反证：只读 b（不走连通分量）会把 3-4 错编成 1-2——这正是增量必须避免的。"""
        S = "sess-split"
        a = self.tmp / "a.jsonl"
        b = self.tmp / "b.jsonl"
        _write(a, [
            _line(S, "u1", "user", "问1", "2026-06-01T00:00:01Z"),
            _line(S, "a1", "assistant", "答1", "2026-06-01T00:00:02Z"),
        ])
        _write(b, [
            _line(S, "u2", "user", "问2", "2026-06-02T00:00:01Z"),
            _line(S, "a2", "assistant", "答2", "2026-06-02T00:00:02Z"),
        ])
        _ingest(self.conn, [a, b])
        self.assertEqual(_rounds(self.conn, S), [(1, 1), (1, 2), (2, 1), (2, 2)])
        # 只喂 b：assign_rounds 从 1 起编，DO UPDATE 把 S 的 (2,*) 覆盖成 (1,*) → 坏。
        _ingest(self.conn, [b])
        self.assertEqual(_rounds(self.conn, S), [(1, 1), (1, 1), (1, 2), (1, 2)])

    def test_independent_file_not_pulled(self) -> None:
        """X、Y 各自单文件独立；只动 X，增量不该顺带重读 Y。"""
        x = self.tmp / "x.jsonl"
        y = self.tmp / "y.jsonl"
        _write(x, [_line("X", "x1", "user", "x问", "2026-06-01T00:00:01Z"),
                   _line("X", "x2", "assistant", "x答", "2026-06-01T00:00:02Z")])
        _write(y, [_line("Y", "y1", "user", "y问", "2026-06-01T00:00:01Z"),
                   _line("Y", "y2", "assistant", "y答", "2026-06-01T00:00:02Z")])
        _ingest(self.conn, [x, y])
        state = current_mtimes([x, y])
        os.utime(x, (state[str(x)] + 10, state[str(x)] + 10))
        selected, _ = select_incremental(self.conn, [x, y], state)
        self.assertEqual({p.name for p in selected}, {"x.jsonl"})

    def test_fork_child_independent_parent_untouched(self) -> None:
        """P 与其 fork 子 C 是两个独立 session；只动 C 时父文件 P 不被重读，P 轮次不变。"""
        P = "parent"
        C = "child"
        pf = self.tmp / "p.jsonl"
        cf = self.tmp / "c.jsonl"
        _write(pf, [
            _line(P, "p-u1", "user", "P问1", "2026-06-01T00:00:01Z"),
            _line(P, "p-a1", "assistant", "P答1", "2026-06-01T00:00:02Z"),
            _line(P, "p-u2", "user", "P问2", "2026-06-01T00:00:03Z"),
            _line(P, "p-a2", "assistant", "P答2", "2026-06-01T00:00:04Z"),
        ])
        # 子文件重放 P 的前两条（同 source_uuid，但 sessionId=C），再接自己的新内容。
        _write(cf, [
            _line(C, "p-u1", "user", "P问1", "2026-06-02T00:00:01Z"),
            _line(C, "p-a1", "assistant", "P答1", "2026-06-02T00:00:02Z"),
            _line(C, "c-u1", "user", "C新问", "2026-06-02T00:00:03Z"),
            _line(C, "c-a1", "assistant", "C新答", "2026-06-02T00:00:04Z"),
        ])
        _ingest(self.conn, [pf, cf])
        self.assertEqual(_rounds(self.conn, P), [(1, 1), (1, 2), (2, 1), (2, 2)])
        self.assertEqual(_rounds(self.conn, C), [(1, 1), (1, 2), (2, 1), (2, 2)])

        state = current_mtimes([pf, cf])
        os.utime(cf, (state[str(cf)] + 10, state[str(cf)] + 10))  # 只有子文件变
        selected, _ = select_incremental(self.conn, [pf, cf], state)
        self.assertEqual({p.name for p in selected}, {"c.jsonl"})  # 不牵连父文件

        _ingest(self.conn, selected)
        self.assertEqual(_rounds(self.conn, P), [(1, 1), (1, 2), (2, 1), (2, 2)])
        self.assertEqual(_rounds(self.conn, C), [(1, 1), (1, 2), (2, 1), (2, 2)])

    def test_no_change_selects_nothing(self) -> None:
        x = self.tmp / "x.jsonl"
        _write(x, [_line("X", "x1", "user", "问", "2026-06-01T00:00:01Z"),
                   _line("X", "x2", "assistant", "答", "2026-06-01T00:00:02Z")])
        _ingest(self.conn, [x])
        state = current_mtimes([x])
        selected, _ = select_incremental(self.conn, [x], state)
        self.assertEqual(selected, [])

    def test_first_run_reads_everything(self) -> None:
        """状态为空（首跑）→ 所有文件都算变化 → 退化为全量。"""
        x = self.tmp / "x.jsonl"
        y = self.tmp / "y.jsonl"
        _write(x, [_line("X", "x1", "user", "问", "2026-06-01T00:00:01Z")])
        _write(y, [_line("Y", "y1", "user", "问", "2026-06-01T00:00:01Z")])
        selected, _ = select_incremental(self.conn, [x, y], {})
        self.assertEqual({p.name for p in selected}, {"x.jsonl", "y.jsonl"})


if __name__ == "__main__":
    unittest.main()
