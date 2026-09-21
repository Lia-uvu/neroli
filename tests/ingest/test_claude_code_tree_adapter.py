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

    def test_meta_errors_and_tool_results_never_become_portable_messages(self) -> None:
        path = self.root / "structural.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "meta", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "isMeta": True,
                    "message": {
                        "role": "user",
                        "content": "Base directory for this skill: private runtime prompt",
                    },
                },
                {
                    "type": "assistant", "uuid": "error", "parentUuid": "meta",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:01Z",
                    "isApiErrorMessage": True,
                    "error": "provider failure",
                    "message": {"role": "assistant", "content": "retry notice"},
                },
                {
                    "type": "user", "uuid": "tool", "parentUuid": "error",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:02Z",
                    "toolUseResult": {"stdout": "private output"},
                    "message": {"role": "user", "content": "tool result wrapper"},
                },
                {
                    "type": "user", "uuid": "user", "parentUuid": "tool",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:03Z",
                    "message": {"role": "user", "content": "actual question"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(by_id["meta"]["kind"], "event")
        self.assertEqual(by_id["error"]["kind"], "event")
        self.assertEqual(by_id["tool"]["kind"], "tool")
        for node_id in ("meta", "error", "tool"):
            self.assertNotIn("message", by_id[node_id])
        self.assertEqual(by_id["user"]["kind"], "message")
        self.assertEqual(
            by_id["user"]["message"]["content"],
            [{"type": "text", "text": "actual question"}],
        )
        self.assertNotIn("private runtime prompt", json.dumps(envelope))
        self.assertNotIn("private output", json.dumps(envelope))

    def test_successful_api_retry_is_a_linear_conversation_not_a_fork(self) -> None:
        path = self.root / "retry.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "question", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "question"},
                },
                {
                    "type": "system", "subtype": "api_error", "uuid": "error",
                    "parentUuid": "question", "sessionId": "session",
                    "timestamp": "2026-01-01T00:00:01Z", "retryAttempt": 1,
                },
                {
                    "type": "assistant", "uuid": "answer", "parentUuid": "question",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "assistant", "content": "answer"},
                },
                {
                    "type": "system", "subtype": "stop_hook_summary", "uuid": "hook",
                    "parentUuid": "error", "sessionId": "session",
                    "timestamp": "2026-01-01T00:00:03Z",
                },
                {
                    "type": "user", "uuid": "follow-up", "parentUuid": "hook",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:04Z",
                    "message": {"role": "user", "content": "follow-up"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        parents = {
            node["native_node_id"]: node["native_parent_node_id"]
            for node in envelope["nodes"]
        }
        self.assertEqual(
            parents,
            {
                "question": None,
                "error": "question",
                "answer": "error",
                "hook": "answer",
                "follow-up": "hook",
            },
        )

    def test_phone_error_keeps_attempt_structural_but_not_portable(self) -> None:
        path = self.root / "phone-20260811-1444-deadbeef.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "failed-user", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-08-11T06:44:00Z",
                    "message": {"role": "user", "content": "please retry me"},
                },
                {
                    "type": "phone-error", "uuid": "error", "parentUuid": "failed-user",
                    "sessionId": "session", "timestamp": "2026-08-11T06:44:01Z",
                    "error": "EOF",
                },
                {
                    "type": "user", "uuid": "delivered-user", "parentUuid": "failed-user",
                    "sessionId": "session", "timestamp": "2026-08-11T06:44:02Z",
                    "message": {"role": "user", "content": "please retry me"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="loft")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}

        self.assertEqual("event", by_id["failed-user"]["kind"])
        self.assertEqual("user:phone-failed-attempt", by_id["failed-user"]["source_type"])
        self.assertNotIn("message", by_id["failed-user"])
        self.assertEqual("message", by_id["delivered-user"]["kind"])

    def test_successful_retry_without_stop_hook_still_follows_the_error(self) -> None:
        path = self.root / "retry-at-end.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "question", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "question"},
                },
                {
                    "type": "system", "subtype": "api_error", "uuid": "error",
                    "parentUuid": "question", "sessionId": "session",
                    "timestamp": "2026-01-01T00:00:01Z",
                },
                {
                    "type": "assistant", "uuid": "answer", "parentUuid": "question",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "assistant", "content": "answer"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(by_id["answer"]["native_parent_node_id"], "error")

    def test_compact_boundary_uses_explicit_logical_parent(self) -> None:
        path = self.root / "compact.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "assistant", "uuid": "old-leaf", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "assistant", "content": "old answer"},
                },
                {
                    "type": "system", "subtype": "compact_boundary",
                    "uuid": "boundary", "parentUuid": None,
                    "logicalParentUuid": "old-leaf", "sessionId": "session",
                    "timestamp": "2026-01-01T00:00:02Z",
                },
                {
                    "type": "user", "uuid": "summary", "parentUuid": "boundary",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:01Z",
                    "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
                    "message": {"role": "user", "content": "runtime summary"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(by_id["boundary"]["native_parent_node_id"], "old-leaf")
        self.assertEqual(by_id["summary"]["native_parent_node_id"], "boundary")

    def test_real_user_continuation_sibling_is_not_flattened(self) -> None:
        path = self.root / "real-branch.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user", "uuid": "question", "parentUuid": None,
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "question"},
                },
                {
                    "type": "assistant", "uuid": "answer", "parentUuid": "question",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:01Z",
                    "message": {"role": "assistant", "content": "answer"},
                },
                {
                    "type": "user", "uuid": "alternate", "parentUuid": "question",
                    "sessionId": "session", "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "user", "content": "alternate continuation"},
                },
            ],
        )

        envelope = export_claude_code_tree([path], room="den")
        by_id = {node["native_node_id"]: node for node in envelope["nodes"]}
        self.assertEqual(by_id["answer"]["native_parent_node_id"], "question")
        self.assertEqual(by_id["alternate"]["native_parent_node_id"], "question")


if __name__ == "__main__":
    unittest.main()
