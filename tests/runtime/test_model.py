"""Model runner retry semantics."""
from __future__ import annotations

import subprocess
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from model import CLIModel, _build_codex_cmd_with_effort, default_codex_command


class CLIModelSuccessArtifactTest(unittest.TestCase):
    def test_empty_stdout_with_success_artifact_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "submission.json"

            def run_once(*_args, **_kwargs):
                artifact.write_text('{"digest":"ok","constants":[]}', encoding="utf-8")
                return subprocess.CompletedProcess(["fake"], 0, stdout="", stderr="")

            runner = CLIModel("fake", cwd=tmp, success_artifact="submission.json")
            with mock.patch("model.subprocess.run", side_effect=run_once) as run, \
                    mock.patch("model.time.sleep") as sleep:
                output = runner.run("prompt")

            self.assertEqual(output, "(success artifact: submission.json)")
            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()

    def test_empty_stdout_without_artifact_keeps_existing_retry_behavior(self):
        result = subprocess.CompletedProcess(["fake"], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            runner = CLIModel("fake", cwd=tmp, success_artifact="submission.json")
            with mock.patch("model.subprocess.run", return_value=result) as run, \
                    mock.patch("model.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "empty output"):
                    runner.run("prompt")

            self.assertEqual(run.call_count, 4)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 120])

    def test_nonzero_exit_with_success_artifact_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "submission.json"

            def submitted_then_cleanup_failed(*_args, **_kwargs):
                artifact.write_text('{"digest":"ok","constants":[]}', encoding="utf-8")
                return subprocess.CompletedProcess(
                    ["fake"], 1, stdout="", stderr="telemetry cleanup failed"
                )

            runner = CLIModel("fake", cwd=tmp, success_artifact="submission.json")
            with mock.patch("model.subprocess.run", side_effect=submitted_then_cleanup_failed) as run, \
                    mock.patch("model.time.sleep") as sleep:
                output = runner.run("prompt")

            self.assertEqual(output, "(success artifact: submission.json)")
            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(runner.last_attempts[0]["status"], "exit_nonzero")

    def test_timeout_with_success_artifact_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "submission.json"

            def submitted_then_timed_out(*_args, **_kwargs):
                artifact.write_text('{"digest":"ok","constants":[]}', encoding="utf-8")
                raise subprocess.TimeoutExpired(["fake"], 300, output="", stderr="hung after submit")

            runner = CLIModel("fake", cwd=tmp, success_artifact="submission.json")
            with mock.patch("model.subprocess.run", side_effect=submitted_then_timed_out) as run, \
                    mock.patch("model.time.sleep") as sleep:
                output = runner.run("prompt")

            self.assertEqual(output, "(success artifact: submission.json)")
            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(runner.last_attempts[0]["status"], "timeout")


class CodexCommandTest(unittest.TestCase):
    def assert_uses_minimal_instructions(self, command: str):
        argv = shlex.split(command)
        overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
        instruction_overrides = [
            value for value in overrides if value.startswith("model_instructions_file=")
        ]

        self.assertEqual(len(instruction_overrides), 1)
        path = Path(instruction_overrides[0].split("=", 1)[1].strip('"'))
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.read_text(encoding="utf-8"), ".\n")

    def test_effort_command_replaces_codex_base_instructions(self):
        self.assert_uses_minimal_instructions(
            _build_codex_cmd_with_effort("gpt-test", "low")
        )

    def test_default_command_replaces_codex_base_instructions(self):
        self.assert_uses_minimal_instructions(default_codex_command("gpt-test"))


if __name__ == "__main__":
    unittest.main()
