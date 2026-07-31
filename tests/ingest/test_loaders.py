import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from loaders import load_messages_for_ingest  # noqa: E402


class LoaderCharacterizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _jsonl(self, name, rows):
        path = self.root / name
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def test_claude_jsonl_keeps_native_identity_and_filters_transport_noise(self):
        path = self._jsonl(
            "session.jsonl",
            [
                {
                    "uuid": "u1",
                    "sessionId": "s1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<system-reminder>noise</system-reminder>\nhello"},
                            {"type": "tool_result", "content": "private command output"},
                            {"type": "image", "source": {"type": "base64"}},
                        ],
                    },
                },
                {
                    "uuid": "ignored-tool",
                    "sessionId": "s1",
                    "toolUseResult": {"stdout": "noise"},
                    "message": {"role": "user", "content": "must not ingest"},
                },
                {
                    "uuid": "ignored-meta",
                    "sessionId": "s1",
                    "isMeta": True,
                    "message": {"role": "user", "content": "must not ingest"},
                },
                {
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "sessionId": "s1",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "model": "claude-opus-4-6",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "text", "text": "hi back"},
                            {"type": "tool_use", "name": "shell"},
                        ],
                    },
                },
            ],
        )

        messages = load_messages_for_ingest([path])

        self.assertEqual(["u1", "a1"], [m.source_uuid for m in messages])
        self.assertEqual(["s1", "s1"], [m.session_id for m in messages])
        self.assertEqual(["hello", "hi back"], [m.text for m in messages])
        self.assertEqual([1, 1], [m.round for m in messages])
        self.assertEqual([1, 2], [m.message_seq for m in messages])
        self.assertEqual(1, messages[0].image_count)
        self.assertEqual("u1", messages[1].parent_uuid)
        self.assertEqual("opus-4.6", messages[1].source)
        self.assertEqual("claude-opus-4-6", messages[1].model)

    def test_command_echoes_are_dropped_but_sentence_arguments_are_kept(self):
        path = self._jsonl(
            "commands.jsonl",
            [
                {
                    "uuid": "bare",
                    "sessionId": "s1",
                    "message": {
                        "role": "user",
                        "content": "<command-name>/model</command-name><command-args>opus</command-args>",
                    },
                },
                {
                    "uuid": "goal",
                    "sessionId": "s1",
                    "message": {
                        "role": "user",
                        "content": "<command-name>/goal</command-name><command-args>please keep this sentence</command-args>",
                    },
                },
                {
                    "uuid": "stdout",
                    "sessionId": "s1",
                    "message": {"role": "user", "content": "<local-command-stdout>Goal set</local-command-stdout>"},
                },
            ],
        )

        messages = load_messages_for_ingest([path])

        self.assertEqual(["goal"], [m.source_uuid for m in messages])
        self.assertEqual("please keep this sentence", messages[0].text)

    def test_export_overlap_dedupes_by_session_and_native_message_uuid(self):
        conversation = {
            "uuid": "conversation-1",
            "chat_messages": [
                {
                    "uuid": "m2",
                    "sender": "assistant",
                    "created_at": "2026-01-01T00:00:02Z",
                    "content": [{"type": "text", "text": "second"}],
                },
                {
                    "uuid": "m1",
                    "sender": "human",
                    "created_at": "2026-01-01T00:00:01Z",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "tool_result", "content": "noise"},
                    ],
                },
            ],
        }
        first = self.root / "export-a.json"
        second = self.root / "export-b.json"
        payload = json.dumps([conversation], ensure_ascii=False)
        first.write_text(payload, encoding="utf-8")
        second.write_text(payload, encoding="utf-8")

        messages = load_messages_for_ingest([second, first])

        self.assertEqual(["m1", "m2"], [m.source_uuid for m in messages])
        self.assertEqual(["conversation-1", "conversation-1"], [m.session_id for m in messages])
        self.assertEqual(["first", "second"], [m.text for m in messages])
        self.assertEqual([1, 1], [m.round for m in messages])

    def test_normalized_ids_are_deterministic_for_the_same_source_path(self):
        path = self.root / "normalized.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "role": "user",
                        "round": 7,
                        "text": "stable",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "session_id": "normalized-session",
                    }
                ]
            ),
            encoding="utf-8",
        )

        first = load_messages_for_ingest([path])
        second = load_messages_for_ingest([path])

        self.assertEqual(first[0].source_uuid, second[0].source_uuid)
        self.assertTrue(first[0].source_uuid.startswith("normalized:"))
        self.assertEqual("normalized-session", first[0].session_id)
        self.assertEqual(7, first[0].round)

    def test_normalized_native_identity_survives_spool_path_changes(self):
        item = {
            "role": "assistant",
            "round": 1,
            "text": "stable reply",
            "timestamp": "2026-07-31T12:00:00Z",
            "session_id": "porch-session",
            "source": "porch",
            "source_native_id": "entry-1",
            "source_parent_id": "entry-0",
            "source_model": "provider/model",
        }
        left = self.root / "left.json"
        right = self.root / "right.json"
        left.write_text(json.dumps([item]), encoding="utf-8")
        right.write_text(json.dumps([item]), encoding="utf-8")

        left_message = load_messages_for_ingest([left])[0]
        right_message = load_messages_for_ingest([right])[0]

        self.assertEqual(left_message.source_uuid, right_message.source_uuid)
        self.assertEqual(left_message.parent_uuid, right_message.parent_uuid)
        self.assertEqual(left_message.source, "porch")
        self.assertEqual(left_message.model, "provider/model")
