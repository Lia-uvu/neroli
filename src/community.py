"""Leiden index Stage 4: recursive Leiden with adaptive depth (design-leiden.html).

Run Leiden on the full graph for top communities, then recurse INTO each:
on the community's induced subgraph, run Leiden again and ask "is this split
real?". The answer drives depth -- a coherent region (求职, one line) stops
shallow; a grab-bag (撒娇, mixed) keeps splitting.

Stopping criterion (replaces v1's size-based _refine): a split is real iff its
modularity significantly exceeds what a structureless graph of the same degree
sequence would score. We estimate that null by degree-preserving rewiring of the
subgraph and running Leiden on each rewire; split iff
    Q_observed > mean(Q_null) + Z * std(Q_null).
Modularity already nets out the null in expectation, but Leiden finds spurious
structure even in random graphs (Q_null > 0), so the ensemble is the honest bar.
The single knob is Z (sensitivity), set once -- not a per-dataset threshold.

Output: a forest of nested dicts, leaves carry the final card grouping.
  {"members": [card_idx...], "level": L, "children": [subtree...]}  # [] = leaf
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

import igraph  # noqa: E402  (vendored)
import leidenalg  # noqa: E402

from config import load_settings  # noqa: E402
from db import connect  # noqa: E402
from embedding import STOP  # noqa: E402
from graph import build_graph  # noqa: E402

# 递归 Leiden 参数在 settings.index；这里的字面量只是缺省。
_idx = load_settings().get("index", {})
MIN_SPLIT_SIZE = _idx.get("split_min_size", 8)  # don't attempt to split a community smaller than this
R_NULL = _idx.get("null_rewires", 20)           # rewired null graphs per significance test
Z = _idx.get("split_z", 2.0)                    # split iff Q_obs > mean(Q_null) + Z*std(Q_null)
SEED = _idx.get("leiden_seed", 15)              # pins the validated 117-cluster / 82-leaf hierarchy


def _leiden(g: igraph.Graph) -> leidenalg.VertexPartition:
    return leidenalg.find_partition(
        g, leidenalg.ModularityVertexPartition, weights="weight", seed=SEED
    )


def _significant(sub: igraph.Graph, q_obs: float) -> bool:
    """True iff q_obs clears the rewired-null bar (mean + Z*std)."""
    if sub.ecount() < MIN_SPLIT_SIZE:
        return False
    weights = sub.es["weight"]  # rewire() drops edge attrs; reassign the multiset
    nulls = []
    for r in range(R_NULL):
        h = sub.copy()
        h.rewire(n=10 * h.ecount())  # degree-preserving edge swaps (topology only)
        h.es["weight"] = weights
        nulls.append(_leiden(h).modularity)
    return q_obs > mean(nulls) + Z * pstdev(nulls)


def _build_igraph(n: int, edges: list[tuple[int, int, float]]) -> igraph.Graph:
    g = igraph.Graph(n=n)
    g.add_edges([(i, j) for i, j, _ in edges])
    g.es["weight"] = [w for _, _, w in edges]
    return g


def _recurse(full: igraph.Graph, node_idxs: list[int], level: int) -> dict:
    leaf = {"members": node_idxs, "level": level, "children": []}
    if len(node_idxs) < MIN_SPLIT_SIZE:
        return leaf
    sub = full.induced_subgraph(node_idxs)
    part = _leiden(sub)
    if len(part) < 2 or not _significant(sub, part.modularity):
        return leaf
    children = []
    for comm in part:
        child_global = [node_idxs[i] for i in comm]
        children.append(_recurse(full, child_global, level + 1))
    return {"members": node_idxs, "level": level, "children": children}


def build_hierarchy(ids: list[str], edges: list[tuple[int, int, float]]) -> list[dict]:
    """Top-level Leiden split (always taken), then adaptive recursion into each."""
    if not ids:
        return []
    if len(ids) < MIN_SPLIT_SIZE:
        return [{"members": list(range(len(ids))), "level": 1, "children": []}]
    igraph.set_random_number_generator(random.Random(SEED))
    full = _build_igraph(len(ids), edges)
    top = _leiden(full)
    return [_recurse(full, list(comm), level=1) for comm in top]


# ---- Stage 5: persist hierarchy + membership ----

SECONDARY_AFFINITY = 0.15   # min fraction of a card's edges into another leaf to flag secondary
MAX_MEMB = 3                # primary + up to (MAX_MEMB-1) secondaries


def _flatten(forest: list[dict]) -> list[tuple[str, str | None, dict]]:
    """DFS-assign cluster_ids; return (cluster_id, parent_id, node) in pre-order."""
    out: list[tuple[str, str | None, dict]] = []
    counter = [0]

    def visit(node: dict, parent_id: str | None) -> None:
        counter[0] += 1
        cid = f"c{counter[0]:04d}"
        out.append((cid, parent_id, node))
        for ch in node["children"]:
            visit(ch, cid)

    for root in forest:
        visit(root, None)
    return out


def _leaf_of(forest: list[dict], flat: list[tuple[str, str | None, dict]]) -> dict[int, str]:
    """card index -> its leaf cluster_id (hard partition)."""
    leaf_map: dict[int, str] = {}
    for cid, _parent, node in flat:
        if not node["children"]:
            for idx in node["members"]:
                leaf_map[idx] = cid
    return leaf_map


def store_hierarchy(
    conn,
    ids: list[str],
    forest: list[dict],
    edges: list[tuple[int, int, float]],
) -> dict:
    """Persist clusters (with parent/level) + cluster_members (primary leaf + secondary affinity)."""
    flat = _flatten(forest)
    leaf_of = _leaf_of(forest, flat)

    # adjacency for secondary affinity
    adj: dict[int, list[int]] = {i: [] for i in range(len(ids))}
    for i, j, _w in edges:
        adj[i].append(j)
        adj[j].append(i)

    conn.execute("DELETE FROM cluster_members")
    conn.execute("DELETE FROM clusters")

    # clusters: every tree node (internal = navigation, leaf = container)
    for cid, parent_id, node in flat:
        summary = _label(conn, ids, node["members"], 4)
        conn.execute(
            "INSERT INTO clusters (cluster_id, summary, parent_cluster_id, level) VALUES (?, ?, ?, ?)",
            (cid, summary, parent_id, node["level"]),
        )

    # primary: each card -> its leaf cluster
    for idx, cid in leaf_of.items():
        conn.execute(
            "INSERT OR IGNORE INTO cluster_members (card_id, cluster_id, role) VALUES (?, ?, 'primary')",
            (ids[idx], cid),
        )

    # secondary: fraction of a card's edges landing in OTHER leaves
    sec_count = 0
    for idx in range(len(ids)):
        nbrs = adj[idx]
        if not nbrs:
            continue
        primary = leaf_of.get(idx)
        tally: dict[str, int] = {}
        for j in nbrs:
            lc = leaf_of.get(j)
            if lc and lc != primary:
                tally[lc] = tally.get(lc, 0) + 1
        total = len(nbrs)
        ranked = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
        for lc, cnt in ranked[: MAX_MEMB - 1]:
            if cnt / total >= SECONDARY_AFFINITY:
                conn.execute(
                    "INSERT OR IGNORE INTO cluster_members (card_id, cluster_id, role) VALUES (?, ?, 'secondary')",
                    (ids[idx], lc),
                )
                sec_count += 1

    conn.commit()
    return {
        "clusters": len(flat),
        "leaves": sum(1 for _, _, n in flat if not n["children"]),
        "primary": len(leaf_of),
        "secondary": sec_count,
    }


# ---- inspection helpers ----

def _leaves(tree: dict) -> list[dict]:
    if not tree["children"]:
        return [tree]
    out = []
    for ch in tree["children"]:
        out.extend(_leaves(ch))
    return out


def _label(conn, ids: list[str], member_idxs: list[int], topn: int = 5) -> str:
    card_ids = [ids[i] for i in member_idxs]
    ph = ",".join("?" for _ in card_ids)
    stop = sorted(STOP)
    stop_ph = ",".join("?" for _ in stop)
    rows = conn.execute(
        f"""
        SELECT e.canonical_name AS ent, COUNT(*) AS c
        FROM card_tags ct
        JOIN tag_entity_map m ON m.tag = ct.tag
        JOIN entities e ON e.entity_id = m.entity_id
        WHERE ct.card_id IN ({ph})
          AND lower(e.canonical_name) NOT IN ({stop_ph})
        GROUP BY e.entity_id ORDER BY c DESC LIMIT ?
        """,
        (*card_ids, *stop, topn),
    ).fetchall()
    if rows:
        return " ".join(f"{r['ent']}({r['c']})" for r in rows)

    rows = conn.execute(
        f"""
        SELECT e.canonical_name AS ent, COUNT(*) AS c
        FROM card_tags ct
        JOIN tag_entity_map m ON m.tag = ct.tag
        JOIN entities e ON e.entity_id = m.entity_id
        WHERE ct.card_id IN ({ph})
        GROUP BY e.entity_id ORDER BY c DESC LIMIT ?
        """,
        (*card_ids, topn),
    ).fetchall()
    return " ".join(f"{r['ent']}({r['c']})" for r in rows) or "(no entities)"


def _print_tree(conn, ids, tree, indent=0):
    members = tree["members"]
    head = "  " * indent + f"L{tree['level']} n={len(members)}"
    if tree["children"]:
        print(f"{head}  [{_label(conn, ids, members, 3)}]")
        for ch in tree["children"]:
            _print_tree(conn, ids, ch, indent + 1)
    else:
        print(f"{head}  {_label(conn, ids, members)}")


if __name__ == "__main__":
    conn = connect()
    ids, edges = build_graph(conn)
    forest = build_hierarchy(ids, edges)

    leaves = [lf for tree in forest for lf in _leaves(tree)]
    sizes = sorted((len(lf["members"]) for lf in leaves), reverse=True)
    depths = [lf["level"] for lf in leaves]
    print(f"\ntop communities: {len(forest)}")
    print(f"leaf clusters:   {len(leaves)}")
    print(f"leaf size  min/median/max: {sizes[-1]}/{sizes[len(sizes)//2]}/{sizes[0]}")
    print(f"leaf depth min/median/max: {min(depths)}/{sorted(depths)[len(depths)//2]}/{max(depths)}")
    print(f"singletons: {sum(1 for s in sizes if s == 1)}")
    print("\n=== hierarchy ===")
    for tree in sorted(forest, key=lambda t: len(t["members"]), reverse=True):
        _print_tree(conn, ids, tree)

    if "--store" in sys.argv:
        stats = store_hierarchy(conn, ids, forest, edges)
        print(f"\nstored: {stats}")
