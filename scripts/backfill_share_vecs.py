"""预热跨房间语义检索用的 share-only 向量缓存。

semantic_search（retrieval.py）对跨房间的卡只用 theme+share 文本的向量——
全文向量含 private，拿去跨房间排序等于让 private 参与泄露。这些 share 向量
Leiden 建树不算，得单独预热；本脚本把缺的补齐。

只处理有 share 的卡（无 share 的卡跨房间本来就不可见）。已缓存的不重算，
幂等可重复跑；nightly 会自动补齐新卡缺少的向量。

用法：python3 scripts/backfill_share_vecs.py [--dry-run]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as dbm
from config import load_settings
from embedding import _cache_key, get_embedding


def share_text(c) -> str:
    """跨房间匹配用的文本。必须和 retrieval.semantic_search 里的构造完全一致。"""
    return "\n".join(p for p in [c["theme"] or "", c["share"] or ""] if p).strip()


def main() -> None:
    dry = "--dry-run" in sys.argv
    emb = load_settings().get("embedding", {})
    backend = emb.get("backend", "api")
    model = emb.get("model") or ("nomic-embed-text" if backend == "ollama" else "BAAI/bge-m3")

    conn = dbm.connect()
    rows = conn.execute(
        "SELECT card_id, theme, share FROM cards"
        " WHERE NULLIF(TRIM(COALESCE(share, '')), '') IS NOT NULL"
    ).fetchall()

    todo = [(r["card_id"], t) for r in rows if (t := share_text(r)) and not _cache_key(backend, model, t).exists()]
    print(f"有 share 的卡 {len(rows)} 张，缺 share 向量 {len(todo)} 张（backend={backend}, model={model}）")
    if dry or not todo:
        return

    ok = fail = 0
    for i, (card_id, text) in enumerate(todo, 1):
        try:
            get_embedding(text, backend=backend, model=model)
            ok += 1
        except Exception as e:  # noqa: BLE001 —— 单卡失败不该中断整批，重跑补漏
            fail += 1
            print(f"  fail {card_id}: {e}", file=sys.stderr)
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(0.05)
    print(f"done: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
