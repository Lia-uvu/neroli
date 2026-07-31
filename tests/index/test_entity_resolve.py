"""Entity judge output must cover the full batch before any mapping is written."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from entity_resolve import _parse_judge_output  # noqa: E402


class EntityJudgeParseTest(unittest.TestCase):
    ITEMS = [("求职", ["找工作", "简历"]), ("晨间散步", ["散步"])]

    def test_complete_batch_parses(self):
        out = (
            '[{"tag":"求职","merge_into":"找工作"},'
            '{"tag":"晨间散步","merge_into":null}]'
        )
        self.assertEqual(
            _parse_judge_output(out, self.ITEMS),
            {"求职": "找工作", "晨间散步": None},
        )

    def test_missing_tag_is_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, "omitted tags"):
            _parse_judge_output(
                '[{"tag":"求职","merge_into":"找工作"}]', self.ITEMS
            )

    def test_invalid_candidate_is_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, "invalid candidate"):
            _parse_judge_output(
                '[{"tag":"求职","merge_into":"不存在"},'
                '{"tag":"晨间散步","merge_into":null}]',
                self.ITEMS,
            )


if __name__ == "__main__":
    unittest.main()
