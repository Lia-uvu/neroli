"""Temporal lens on the Leiden tree: recent-active vs long-term background views.

Overlays time signals onto the existing semantic hierarchy without modifying it.

  python src/tempo.py              # default 7-day window
  python src/tempo.py --window 14  # 14-day window
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import connect  # noqa: E402
from timefmt import format_local_timestamp  # noqa: E402


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _local(dt: datetime) -> str:
    return format_local_timestamp(dt.isoformat())


def temporal_lens(conn, window_days: int = 7) -> dict:
    """Returns {leaf_id: metrics} and {root_id: aggregated} for the two views."""

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    # global observation window
    bounds = conn.execute(
        "SELECT MIN(timestamp) AS t0, MAX(timestamp) AS t1 FROM cards WHERE timestamp IS NOT NULL"
    ).fetchone()
    t_min, t_max = _parse(bounds["t0"]), _parse(bounds["t1"])
    total_days = (t_max - t_min).days or 1
    expected_ratio = window_days / total_days

    # identify leaf cluster ids
    leaf_ids = {
        r["cluster_id"]
        for r in conn.execute("""
            SELECT cluster_id FROM clusters
            WHERE cluster_id NOT IN (
                SELECT DISTINCT parent_cluster_id FROM clusters
                WHERE parent_cluster_id IS NOT NULL
            )
        """).fetchall()
    }

    # per-leaf: card timestamps
    leaf_cards = {}
    for lid in leaf_ids:
        rows = conn.execute("""
            SELECT c.timestamp
            FROM cluster_members cm JOIN cards c ON c.card_id = cm.card_id
            WHERE cm.cluster_id = ? AND cm.role = 'primary' AND c.timestamp IS NOT NULL
            ORDER BY c.timestamp
        """, (lid,)).fetchall()
        ts = sorted(_parse(r["timestamp"]) for r in rows)
        if not ts:
            continue
        total = len(ts)
        recent = sum(1 for t in ts if t >= cutoff)
        span_days = (ts[-1] - ts[0]).days
        spread = span_days / total_days

        actual_ratio = recent / total
        heat = actual_ratio / expected_ratio if expected_ratio else 0

        # density evenness: bin into weeks, CoV (lower = more even)
        week_bins: dict[int, int] = {}
        for t in ts:
            wk = (t - t_min).days // 7
            week_bins[wk] = week_bins.get(wk, 0) + 1
        if spread > 0:
            first_wk = (ts[0] - t_min).days // 7
            last_wk = (ts[-1] - t_min).days // 7
            all_wks = range(first_wk, last_wk + 1)
            counts = [week_bins.get(w, 0) for w in all_wks]
            mean_c = sum(counts) / len(counts) if counts else 0
            var_c = sum((c - mean_c) ** 2 for c in counts) / len(counts) if counts else 0
            cov = (var_c ** 0.5) / mean_c if mean_c > 0 else 999
        else:
            cov = 999

        leaf_cards[lid] = {
            "total": total,
            "recent": recent,
            "spread": spread,
            "heat": heat,
            "cov": cov,
            "t_start": ts[0],
            "t_end": ts[-1],
        }

    # map each leaf → L1 root
    def find_root(cid):
        while True:
            row = conn.execute(
                "SELECT parent_cluster_id FROM clusters WHERE cluster_id = ?", (cid,)
            ).fetchone()
            if not row or not row["parent_cluster_id"]:
                return cid
            cid = row["parent_cluster_id"]

    leaf_root = {lid: find_root(lid) for lid in leaf_cards}

    # cluster summaries lookup
    summaries = {
        r["cluster_id"]: r["summary"]
        for r in conn.execute("SELECT cluster_id, summary FROM clusters").fetchall()
    }

    # aggregate to L1 roots
    root_agg: dict[str, dict] = {}
    for lid, m in leaf_cards.items():
        rid = leaf_root[lid]
        if rid not in root_agg:
            root_agg[rid] = {"total": 0, "recent": 0, "leaves": []}
        root_agg[rid]["total"] += m["total"]
        root_agg[rid]["recent"] += m["recent"]
        root_agg[rid]["leaves"].append(lid)

    for rid, agg in root_agg.items():
        actual_ratio = agg["recent"] / agg["total"] if agg["total"] else 0
        agg["heat"] = actual_ratio / expected_ratio if expected_ratio else 0

    return {
        "leaf": leaf_cards,
        "root": root_agg,
        "leaf_root": leaf_root,
        "summaries": summaries,
        "window_days": window_days,
        "expected_ratio": expected_ratio,
        "total_days": total_days,
    }


def print_recent(data: dict, top_n: int = 15) -> None:
    """View 1: branches lighting up recently."""
    leaf, root_agg, lr, summ = data["leaf"], data["root"], data["leaf_root"], data["summaries"]
    window = data["window_days"]

    print(f"\n{'='*60}")
    print(f" 🔥 近 {window} 天活跃话题（投影到语义树）")
    print(f"{'='*60}")

    # sort roots by heat
    sorted_roots = sorted(root_agg.items(), key=lambda kv: kv[1]["heat"], reverse=True)
    for rid, agg in sorted_roots:
        heat_bar = "▓" * min(int(agg["heat"] * 5), 20) + "░" * max(0, 5 - min(int(agg["heat"] * 5), 5))
        ratio_pct = agg["recent"] / agg["total"] * 100 if agg["total"] else 0
        print(f"\n  {heat_bar} {summ.get(rid, rid)[:40]}")
        print(f"       {agg['recent']}/{agg['total']}卡 ({ratio_pct:.0f}% in {window}d)  heat={agg['heat']:.2f}")

        # top hot leaves under this root
        hot_leaves = sorted(
            [(lid, leaf[lid]) for lid in agg["leaves"]],
            key=lambda x: x[1]["recent"], reverse=True,
        )
        for lid, m in hot_leaves[:4]:
            if m["recent"] == 0:
                continue
            s = summ.get(lid, lid)[:44]
            print(f"         ├ {lid} +{m['recent']}卡  {s}")


def print_background(data: dict, top_n: int = 15) -> None:
    """View 2: stable long-term themes."""
    leaf, summ = data["leaf"], data["summaries"]
    lr = data["leaf_root"]

    print(f"\n{'='*60}")
    print(f" 🪨 长期背景知识（高跨度 + 均匀密度）")
    print(f"{'='*60}")

    # score: spread * (1 / (1 + cov)) * log(total)  — wide + even + substantial
    import math
    scored = []
    for lid, m in leaf.items():
        if m["total"] < 4:
            continue
        stability = m["spread"] * (1 / (1 + m["cov"])) * math.log(m["total"] + 1)
        scored.append((lid, m, stability))
    scored.sort(key=lambda x: x[2], reverse=True)

    for lid, m, score in scored[:top_n]:
        root_id = lr[lid]
        root_label = summ.get(root_id, "")[:20]
        spread_bar = "━" * int(m["spread"] * 20) + "╌" * (20 - int(m["spread"] * 20))
        t0 = _local(m["t_start"])[:10]
        t1 = _local(m["t_end"])[:10]
        print(f"\n  [{spread_bar}] {summ.get(lid, lid)[:44]}")
        print(f"       {m['total']}卡  {t0}~{t1}  spread={m['spread']:.2f}  evenness={1/(1+m['cov']):.2f}")
        print(f"       └ ↖ {root_label}")


def print_drift(data: dict) -> None:
    """View 3: the interesting middle — heating up, cooling down, or steady."""
    leaf, summ, lr = data["leaf"], data["summaries"], data["leaf_root"]
    window = data["window_days"]

    print(f"\n{'='*60}")
    print(f" 📊 话题温度变化")
    print(f"{'='*60}")

    heating = [(lid, m) for lid, m in leaf.items() if m["heat"] > 2.0 and m["recent"] >= 2]
    cooling = [(lid, m) for lid, m in leaf.items() if m["heat"] < 0.3 and m["total"] >= 8 and m["spread"] > 0.3]
    steady = [(lid, m) for lid, m in leaf.items() if 0.6 <= m["heat"] <= 1.5 and m["total"] >= 6 and m["spread"] > 0.5]

    heating.sort(key=lambda x: x[1]["heat"], reverse=True)
    cooling.sort(key=lambda x: x[1]["total"], reverse=True)
    steady.sort(key=lambda x: x[1]["total"], reverse=True)

    print(f"\n  🔺 升温（heat > 2x 均值）")
    for lid, m in heating[:8]:
        print(f"     {lid} heat={m['heat']:.1f}x  +{m['recent']}卡/{m['total']}  {summ.get(lid, '')[:40]}")
    if not heating:
        print("     （无）")

    print(f"\n  🔻 降温（曾经活跃，近 {window} 天沉默）")
    for lid, m in cooling[:8]:
        print(f"     {lid} {m['total']}卡 spread={m['spread']:.2f}  {summ.get(lid, '')[:40]}")
    if not cooling:
        print("     （无）")

    print(f"\n  ━━ 稳定底色（一直在，从不断流）")
    for lid, m in steady[:8]:
        print(f"     {lid} {m['total']}卡 heat={m['heat']:.1f}x  {summ.get(lid, '')[:40]}")
    if not steady:
        print("     （无）")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=7, help="recent window in days")
    args = ap.parse_args()

    conn = connect()
    data = temporal_lens(conn, window_days=args.window)

    print_recent(data)
    print_background(data)
    print_drift(data)
