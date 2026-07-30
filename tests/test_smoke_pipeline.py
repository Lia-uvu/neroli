"""End-to-end smoke test: ingest -> card gen (fake model) -> FTS -> context.

Hermetic regression baseline for the module refactor. Runs against a throwaway
DB and temp room dirs; never touches data/fragments.db or the real room folders.
Card gen uses an injected fake model, so no network / no real LLM call.

Out of scope: the Leiden index (graph/community) needs the embedding API, so it
is not exercised here. This guards the high-churn refactor areas: ingest, card
parse+insert, FTS, and context rebuild.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import context
import db
import retrieval
from memory_types import Message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeModel:
    """Returns a fixed card-format string regardless of prompt."""

    name = "fake:test"

    def __init__(self, output: str) -> None:
        self._output = output
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._output


def _sample_messages(session_id: str, ts: str) -> list[Message]:
    rows = [
        ("user", "Amy", "我在研究记忆管线的聚类算法"),
        ("assistant", "Claude", "可以用 Leiden 社区检测来聚类事件卡"),
        ("user", "Amy", "那向量用 bge-m3 吗"),
        ("assistant", "Claude", "对，bge-m3 做 embedding 然后建 kNN 图"),
    ]
    messages = []
    for i, (role, speaker, text) in enumerate(rows):
        rnd = i // 2 + 1          # 2 messages per round -> rounds 1, 2
        seq = i % 2 + 1           # user=1, assistant=2
        messages.append(Message(
            role=role, speaker=speaker, text=text, timestamp=ts,
            session_id=session_id, seq=i + 1, round=rnd, label=f"{i:02d}",
            source_uuid=f"test-{session_id}-{i}", message_seq=seq, line_no=i,
        ))
    return messages


CARD_OUTPUT = (
    "turns:1-1\n"
    "headline: 讨论用 Leiden 聚类事件卡\n"
    "share: 决定用 Leiden 社区检测聚类\n"
    "private:\n"
    "tags:Leiden/聚类\n"
    "\n"
    "turns:2-2\n"
    "headline: 选 bge-m3 做 embedding\n"
    "share: 用 bge-m3 建 kNN 图\n"
    "private:\n"
    "tags:bge-m3/embedding\n"
)


class SmokePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db_path = tmp / "test.db"
        self.room_dir = tmp / "room"
        self.room_dir.mkdir()
        # Redirect rooms so context writes into temp, not a real agent directory.
        self._saved = (context.ROOMS, dict(context.ROOM_DIRS), dict(context.ROOM_SLUGS))
        context.ROOMS = ("room",)
        context.ROOM_DIRS = {"room": self.room_dir}
        context.ROOM_SLUGS = {"room": "room"}

    def tearDown(self) -> None:
        context.ROOMS, context.ROOM_DIRS, context.ROOM_SLUGS = self._saved
        self._tmp.cleanup()

    def test_end_to_end(self) -> None:
        from pipeline import process_session

        ts = _now_iso()
        session_id = "smoke-session"
        conn = db.connect(self.db_path)

        # --- ingest ---
        new_sessions = db.ingest_turns(conn, _sample_messages(session_id, ts))
        self.assertEqual(new_sessions, [session_id])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 4)

        # --- card gen (fake model) ---
        model = FakeModel(CARD_OUTPUT)
        run_id = db.create_pipeline_run(conn, model.name, None, [])
        changed = process_session(conn, run_id, session_id, model, room="room")
        self.assertTrue(changed)
        self.assertEqual(len(model.prompts), 1)  # 2 rounds -> single window call

        cards = conn.execute(
            "SELECT card_id, headline, share, private FROM cards ORDER BY card_id"
        ).fetchall()
        self.assertEqual(len(cards), 2)
        headlines = {c["headline"] for c in cards}
        self.assertIn("讨论用 Leiden 聚类事件卡", headlines)
        self.assertIn("选 bge-m3 做 embedding", headlines)

        tags = {r["tag"] for r in conn.execute("SELECT tag FROM card_tags").fetchall()}
        self.assertEqual(tags, {"leiden", "聚类", "bge-m3", "embedding"})

        # --- FTS search（生产检索路径：retrieval.search，非 db 旧口） ---
        hits = retrieval.search(conn, "Leiden", viewer="room")
        self.assertTrue(any("Leiden" in h.headline for h in hits))

        # --- context rebuild (into temp room dir) ---
        context.rebuild_context(conn)
        context_file = self.room_dir / "cards-last-24.md"
        self.assertTrue(context_file.exists())
        body = context_file.read_text(encoding="utf-8")
        self.assertIn("讨论用 Leiden 聚类事件卡", body)
        self.assertIn("选 bge-m3 做 embedding", body)
        self.assertIn("（今天）--", body)

        conn.close()


if __name__ == "__main__":
    unittest.main()
