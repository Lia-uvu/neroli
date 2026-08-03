from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402


class TreeCardProjectionPolicyTest(unittest.TestCase):
    def test_missing_policy_projects_every_tree_source(self) -> None:
        with patch.object(config, "load_settings", return_value={}):
            self.assertTrue(config.project_tree_cards_for_source("porch"))
            self.assertTrue(config.project_tree_cards_for_source("claude-code"))

    def test_explicit_policy_can_hold_one_migrating_source_history_only(self) -> None:
        settings = {
            "ingest": {"tree_card_projection_sources": ["porch"]}
        }
        with patch.object(config, "load_settings", return_value=settings):
            self.assertTrue(config.project_tree_cards_for_source("porch"))
            self.assertFalse(config.project_tree_cards_for_source("claude-code"))

    def test_invalid_policy_fails_closed(self) -> None:
        settings = {"ingest": {"tree_card_projection_sources": "porch"}}
        with patch.object(config, "load_settings", return_value=settings):
            with self.assertRaisesRegex(ValueError, "must be an array"):
                config.project_tree_cards_for_source("porch")


if __name__ == "__main__":
    unittest.main()
