import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gen_cards import parse_cards


class ParseCardsTest(unittest.TestCase):
    def test_empty_share_with_markdown_linebreak_stays_empty(self):
        text = (
            "turns:23-35  \n"
            "theme: 亲吻变得更亲密。  \n"
            "share:  \n"
            "private: 只该在 private 里的正文。  \n"
            "tags:后颈/亲吻"
        )

        cards = parse_cards(text)

        self.assertEqual(cards[0]["share"], "")
        self.assertEqual(cards[0]["private"], "只该在 private 里的正文。")
        self.assertEqual(cards[0]["tags"], ["后颈", "亲吻"])

    def test_empty_private_with_markdown_linebreak_does_not_capture_tags(self):
        text = (
            "turns:12-14  \n"
            "theme: Amy 打了一会儿 FF14。  \n"
            "share: 普通事实。  \n"
            "private:  \n"
            "tags:FF14/休息"
        )

        cards = parse_cards(text)

        self.assertEqual(cards[0]["share"], "普通事实。")
        self.assertEqual(cards[0]["private"], "")
        self.assertEqual(cards[0]["tags"], ["FF14", "休息"])


if __name__ == "__main__":
    unittest.main()
