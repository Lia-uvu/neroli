#!/usr/bin/env python3
"""把卡层（cards + tags + raw）整体导出到 JSON。v4 迁移前的一次性保险。

在完好的 v3 库上跑，迁移不动卡层（Option A），但万一以后要 Option B 全量重生，
这份备份能还原任何被误删的卡。用法：

    python3 scripts/backup_cards.py                       # 默认 data/fragments.db → data/backups/cards-v3-backup.json
    python3 scripts/backup_cards.py --db <path> --out <path>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "fragments.db"
DEFAULT_OUT = ROOT / "data" / "backups" / "cards-v3-backup.json"


def backup(db_path: Path, out_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cards = []
    for c in conn.execute("SELECT * FROM cards ORDER BY session_id, turn_start").fetchall():
        card = dict(c)
        card["tags"] = [
            r["tag"] for r in conn.execute(
                "SELECT tag FROM card_tags WHERE card_id = ?", (c["card_id"],)
            ).fetchall()
        ]
        raw = conn.execute(
            "SELECT raw, label, source_file FROM card_raw WHERE card_id = ?", (c["card_id"],)
        ).fetchone()
        card["raw"] = dict(raw) if raw else None
        cards.append(card)
    conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(cards)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup the card layer before the v4 migration.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    n = backup(args.db, args.out)
    print(f"backed up {n} cards -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
