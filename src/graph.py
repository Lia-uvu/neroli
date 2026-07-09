"""Leiden index Stage 3: weighted card-similarity graph (design-leiden.html).

Nodes = event cards. Edge weight blends two signals, both living in the weight:

  - Embedding kNN cosine: mean-centered card-text cosine. Provides connectivity
    -- two semantically close cards link even with no shared entity. Backbone.
  - Entity co-occurrence: IDF-weighted sum over shared canonical entities
    (rare entity weighs more: 文竹 > 植物). Provides topic separation. Resolved
    through tag_entity_map so only df>=2 canonical entities count; very generic
    entities (df > GENERIC_FRAC*N, e.g. the user's name) are dropped like STOP words.

Output is generic -- (card_ids, edges) with edges as (i, j, weight) on node
indices -- so Stage 4 can build igraph/networkx without this module caring which.

CLI: python src/graph.py  -> builds and prints graph stats.
"""
from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from embedding import STOP, card_text, center_embeddings, get_embedding
from config import load_settings
from db import connect

# 建图参数在 settings.index；这里的字面量只是缺省。
_idx = load_settings().get("index", {})
DEFAULT_K = _idx.get("knn_k", 12)              # embedding kNN neighbours per node
GENERIC_FRAC = _idx.get("generic_frac", 0.25)  # entities on > this fraction of cards are treated as STOP
W_EMB = _idx.get("w_emb", 1.0)                 # embedding-signal weight in the blend
W_COOC = _idx.get("w_cooc", 1.0)               # co-occurrence-signal weight in the blend


def load_cards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT card_id, theme, share, private FROM cards ORDER BY card_id"
    ).fetchall()


def load_card_entities(conn: sqlite3.Connection, n_cards: int) -> tuple[dict[str, set[str]], dict[str, float]]:
    """card_id -> set of canonical entities (via tag_entity_map), plus entity IDF.

    Drops STOP entities and over-generic ones (df > GENERIC_FRAC * n_cards).
    """
    rows = conn.execute(
        """
        SELECT ct.card_id AS card_id, e.canonical_name AS ent
        FROM card_tags ct
        JOIN tag_entity_map m ON m.tag = ct.tag
        JOIN entities e ON e.entity_id = m.entity_id
        """
    ).fetchall()
    card_ents: dict[str, set[str]] = defaultdict(set)
    df: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str]] = set()
    for r in rows:
        ent = r["ent"]
        if ent in STOP:
            continue
        key = (r["card_id"], ent)
        if key in seen:
            continue
        seen.add(key)
        card_ents[r["card_id"]].add(ent)
        df[ent] += 1

    df_cap = GENERIC_FRAC * n_cards
    generic = {e for e, d in df.items() if d > df_cap}
    if generic:
        for ents in card_ents.values():
            ents -= generic
    idf = {e: math.log(n_cards / d) for e, d in df.items() if e not in generic}
    return dict(card_ents), idf


def build_graph(
    conn: sqlite3.Connection,
    k: int = DEFAULT_K,
    w_emb: float = W_EMB,
    w_cooc: float = W_COOC,
) -> tuple[list[str], list[tuple[int, int, float]]]:
    cards = load_cards(conn)
    ids = [c["card_id"] for c in cards]
    idx = {cid: i for i, cid in enumerate(ids)}
    n = len(ids)
    if n < 2:
        return ids, []
    k = min(k, n - 1)

    es = load_settings().get("embedding", {})
    backend, model = es.get("backend", "api"), es.get("model", "")
    raw = {c["card_id"]: get_embedding(card_text(c), backend, model) for c in cards}
    centered = center_embeddings(raw)

    mat = np.array([centered[cid] for cid in ids], dtype=np.float32)
    mat /= np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)
    cos = mat @ mat.T  # full mean-centered cosine matrix

    # --- embedding kNN candidate edges (union, undirected) ---
    cand: set[tuple[int, int]] = set()
    for i in range(n):
        row = cos[i].copy()
        row[i] = -np.inf
        nbrs = np.argpartition(row, -k)[-k:]
        for j in nbrs:
            cand.add((min(i, int(j)), max(i, int(j))))

    # --- entity co-occurrence candidate edges + weights ---
    card_ents, idf = load_card_entities(conn, n)
    ent_cards: dict[str, list[int]] = defaultdict(list)
    for cid, ents in card_ents.items():
        for e in ents:
            ent_cards[e].append(idx[cid])
    cooc: dict[tuple[int, int], float] = defaultdict(float)
    for e, members in ent_cards.items():
        w = idf.get(e, 0.0)
        if w <= 0:
            continue
        members = sorted(members)
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                cooc[(members[a], members[b])] += w
    cand |= set(cooc.keys())

    # --- blend signals into one weight per candidate edge ---
    emb_vals = {(i, j): float(cos[i, j]) for (i, j) in cand}
    e_lo, e_hi = min(emb_vals.values()), max(emb_vals.values())
    c_hi = max(cooc.values()) if cooc else 1.0

    def norm(v, lo, hi):
        return (v - lo) / (hi - lo) if hi > lo else 0.0

    edges: list[tuple[int, int, float]] = []
    for (i, j) in cand:
        e_n = norm(emb_vals[(i, j)], e_lo, e_hi)
        c_n = cooc.get((i, j), 0.0) / c_hi if c_hi else 0.0
        weight = w_emb * e_n + w_cooc * c_n
        if weight > 0:
            edges.append((i, j, weight))
    return ids, edges


def _stats(ids: list[str], edges: list[tuple[int, int, float]]) -> None:
    n = len(ids)
    deg = [0] * n
    for i, j, _ in edges:
        deg[i] += 1
        deg[j] += 1
    isolated = sum(1 for d in deg if d == 0)
    # connected components via union-find
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
    comps = len({find(i) for i in range(n)})
    ws = sorted(w for _, _, w in edges)
    print(f"nodes={n} edges={len(edges)}")
    print(f"avg degree={2*len(edges)/n:.1f}  isolated nodes={isolated}  components={comps}")
    if ws:
        print(f"weight min/median/max={ws[0]:.3f}/{ws[len(ws)//2]:.3f}/{ws[-1]:.3f}")
    print(f"degree min/median/max={min(deg)}/{sorted(deg)[n//2]}/{max(deg)}")


if __name__ == "__main__":
    conn = connect()
    k = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_K
    ids, edges = build_graph(conn, k=k)
    _stats(ids, edges)
