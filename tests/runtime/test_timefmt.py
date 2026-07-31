"""Regression tests for settings-driven timestamp rendering."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import timefmt


class TimefmtTest(unittest.TestCase):
    def test_timestamp_uses_current_timezone_setting(self) -> None:
        timestamp = "2026-07-13T09:14:38.549Z"

        with patch.object(timefmt, "load_settings", return_value={"timezone": "UTC"}):
            self.assertEqual(timefmt.format_local_timestamp(timestamp), "2026-07-13 09:14")

        with patch.object(
            timefmt,
            "load_settings",
            return_value={"timezone": "Asia/Shanghai"},
        ):
            self.assertEqual(timefmt.format_local_timestamp(timestamp), "2026-07-13 17:14")
            self.assertEqual(timefmt.format_local_date_time(timestamp), ("2026-07-13", "17:14"))


if __name__ == "__main__":
    unittest.main()
