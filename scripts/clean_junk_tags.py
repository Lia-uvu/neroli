"""Remove obvious sentence-fragment tags (one-time, idempotent).

Background: the card-gen LLM occasionally drops prose fragments / dangling
quotes into the tag field instead of short keywords. These are pure noise.

Targeting rule (deliberately surgical):
  - df == 1 (appears on exactly one card → contributes zero card-to-card
    edges in the v2 similarity graph, so safe to drop), AND
  - contains CJK sentence punctuation or quote marks: 。，！？；：“”

We do NOT delete df=1 tags by length alone: long English kebab-case tags
(event-memory-design, nomic-embed-text, ...) are legit, just specific.
We do NOT touch df>=2 tags here — synonym dedup of those is a separate,
LLM-judged pass (see tag_dedup_experiment.py / v2 Stage 2).

Run with --apply to actually delete; default is dry-run (report only).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import connect

PUNCT = ["。", "，", "！", "？", "；", "：", "“", "”"]


def junk_tags(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT tag, COUNT(DISTINCT card_id) AS df
        FROM card_tags GROUP BY tag HAVING df = 1
        """
    ).fetchall()
    return [r["tag"] for r in rows if any(p in r["tag"] for p in PUNCT)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    args = ap.parse_args()

    conn = connect()
    targets = junk_tags(conn)

    print(f"Found {len(targets)} junk df=1 tags:")
    for t in targets:
        print(f"  - {t}")

    if not targets:
        return
    if not args.apply:
        print("\n(dry-run; pass --apply to delete)")
        return

    placeholders = ",".join("?" for _ in targets)
    cur = conn.execute(
        f"DELETE FROM card_tags WHERE tag IN ({placeholders})", targets
    )
    conn.commit()
    print(f"\nDeleted {cur.rowcount} tag rows.")


if __name__ == "__main__":
    main()
