from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claude_code_adapter import export_claude_code_tree  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class ClaudeCodeTreeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_replay_dedupes_and_compaction_reparent_keeps_original_edge(self) -> None:
        original = self.root / "original.jsonl"
        replay = self.root / "replay.jsonl"
        write_jsonl(
            original,
            [
                {
                    "type": "user", "uuid": "user-1", "parentUuid": None,
                    "sessionId": "session-a", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "question"},
                },
                {
                    "type": "assistant", "uuid": "answer-1", "parentUuid": "user-1",
                    "sessionId": "session-a", "timestamp": "2026-01-01T00:00:01Z",
                    "model": "claude-test",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "answer"},
                        ],
                    },
                },
            ],
        )
        write_jsonl(
            replay,
            [
                {
                    "type": "user", "uuid": "compact-1", "parentUuid": "user-1",
                    "sessionId": "session-b", "timestamp": "2026-01-01T00:00:02Z",
                    "isCompactSummary": True,
                    "message": {"role": "user", "content": "runtime summary"},
                },
                {
                    "type": "assistant", "uuid": "answer-1", "parentUuid": "compact-1",
                    "sessionId": "session-b", "timestamp": "2026-01-01T00:00:01Z",
                    "model": "claude-test",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "answer"},
                        ],
                    },
                },
                {
                    "type": "last-prompt", "sessionId": "session-b",
                    "leafUuid": "answer-1", "lastPrompt": "not exported",
                },
            ],
        )
        os.utime(replay, (1_800_000_000, 1_800_000_000))

        envelope = export_claude_code_tree([replay, original], room="den")

        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(set(by_id), {"user-1", "answer-1", "compact-1"})
        self.assertEqual(by_id["answer-1"]["native_parent_node_id"], "user-1")
        self.assertEqual(by_id["compact-1"]["kind"], "checkpoint")
        self.assertEqual(
            by_id["answer-1"]["message"],
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}],
                "model": "claude-test",
            },
        )
        self.assertEqual(
            envelope["observations"][0],
            {
                "native_context_id": "session-b",
                "kind": "cursor",
                "native_node_id": "answer-1",
                "observed_at": "2027-01-15T08:00:00.000000Z",
                "payload": {"runtime": "claude-code"},
            },
        )
        self.assertNotIn("lastPrompt", json.dumps(envelope))
        self.assertNotIn("private", json.dumps(envelope))

    def test_ambiguous_non_compaction_parent_reuse_is_a_hard_failure(self) -> None:
        paths = []
        for index, parent in enumerate(("parent-a", "parent-b")):
            path = self.root / f"{index}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user", "uuid": parent, "parentUuid": None,
                        "sessionId": f"session-{index}",
                        "timestamp": f"2026-01-01T00:00:0{index}Z",
                        "message": {"role": "user", "content": parent},
                    },
                    {
                        "type": "assistant", "uuid": "reused", "parentUuid": parent,
                        "sessionId": f"session-{index}",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "message": {"role": "assistant", "content": "same"},
                    },
                ],
            )
            paths.append(path)

        with self.assertRaisesRegex(ValueError, "ambiguous parent variants"):
            export_claude_code_tree(paths, room="den")

    def test_same_uuid_with_changed_portable_content_is_a_hard_failure(self) -> None:
        path = self.root / "conflict.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "same", "parentUuid": None,
                    "sessionId": "session-a", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "first"},
                },
                {
                    "type": "user", "uuid": "same", "parentUuid": None,
                    "sessionId": "session-b", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "changed"},
                },
            ],
        )

        with self.assertRaisesRegex(ValueError, "conflicting portable content"):
            export_claude_code_tree([path], room="den")

    def test_preserves_text_blocks_and_whitespace_but_filters_runtime_injection(self) -> None:
        path = self.root / "content.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "assistant", "uuid": "assistant", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "  first block\n"},
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "second block  "},
                        ],
                    },
                },
                {
                    "type": "user", "uuid": "user", "parentUuid": "assistant",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:01Z",
                    "message": {
                        "role": "user",
                        "content": "hello<system-reminder>private runtime note</system-reminder>",
                    },
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(
            by_id["assistant"]["message"]["content"],
            [
                {"type": "text", "text": "  first block\n"},
                {"type": "text", "text": "second block  "},
            ],
        )
        self.assertEqual(
            by_id["user"]["message"]["content"],
            [{"type": "text", "text": "hello"}],
        )
        self.assertNotIn("private", json.dumps(envelope))


if __name__ == "__main__":
    unittest.main()
