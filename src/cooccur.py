"""Tag 共现统计：一阶 PMI + 二阶 profile cosine。

从 pipeline-lab/cluster_lab/cooccur.py 移植。
数据源改为主库的 card_tags 表（不再用独立 cooccur.db）。

一阶共现：两个 tag 同框次数 → PMI（谁和谁绑在一起）。
二阶共现：tag 的 ppmi profile 向量做 cosine → 不靠词库的"近义/相关"。
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict

from embedding import STOP

MIN_DF_VOCAB = 2


def load_matrix(conn: sqlite3.Connection) -> tuple[dict, dict, dict, int]:
    rows = conn.execute(
        """
        SELECT ct.card_id, ct.tag, c.session_id
        FROM card_tags ct
        JOIN cards c ON c.card_id = ct.card_id
        """
    ).fetchall()
    card_tags: dict[str, set[str]] = defaultdict(set)
    sess: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        t = r["tag"]
        if t in STOP:
            continue
        card_tags[r["card_id"]].add(t)
        sess[t].add(r["session_id"])
    df: dict[str, int] = defaultdict(int)
    for tags in card_tags.values():
        for t in tags:
            df[t] += 1
    return dict(card_tags), dict(df), dict(sess), len(card_tags)


def pair_counts(card_tags: dict[str, set[str]]) -> dict[tuple[str, str], int]:
    co: dict[tuple[str, str], int] = defaultdict(int)
    for tags in card_tags.values():
        ts = sorted(tags)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                co[(a, b)] += 1
    return dict(co)


def pmi(a: str, b: str, co: dict, df: dict, N: int) -> tuple[float, int]:
    n_ab = co.get((min(a, b), max(a, b)), 0)
    if n_ab == 0 or df.get(a, 0) == 0 or df.get(b, 0) == 0:
        return 0.0, 0
    p = (n_ab * N) / (df[a] * df[b])
    return math.log2(p), n_ab


def profiles(card_tags: dict, co: dict, df: dict, N: int) -> dict[str, dict[str, float]]:
    vocab = [t for t, d in df.items() if d >= MIN_DF_VOCAB]
    prof: dict[str, dict[str, float]] = {}
    for t in vocab:
        vec: dict[str, float] = {}
        for u in vocab:
            if u == t:
                continue
            val, n = pmi(t, u, co, df, N)
            if val > 0:
                vec[u] = val
        prof[t] = vec
    return prof


def _cos(u: dict, v: dict) -> float:
    if not u or not v:
        return 0.0
    dot = sum(val * v.get(k, 0.0) for k, val in u.items())
    nu = math.sqrt(sum(x * x for x in u.values()))
    nv = math.sqrt(sum(x * x for x in v.values()))
    return dot / (nu * nv) if nu and nv else 0.0


def second_order_near(tag: str, prof: dict, topk: int = 6) -> list[tuple[str, float]]:
    if tag not in prof:
        return []
    out = [(_cos(prof[tag], prof[u]), u) for u in prof if u != tag]
    out.sort(reverse=True)
    return [(u, s) for s, u in out[:topk] if s > 0]


def sim_map(conn: sqlite3.Connection, threshold: float = 0.6) -> dict[tuple[str, str], float]:
    ct, df, sess, N = load_matrix(conn)
    prof = profiles(ct, pair_counts(ct), df, N)
    vocab = list(prof)
    out: dict[tuple[str, str], float] = {}
    for i, a in enumerate(vocab):
        for b in vocab[i + 1:]:
            s = _cos(prof[a], prof[b])
            if s >= threshold:
                out[(a, b)] = s
                out[(b, a)] = s
    return out


def assoc(conn: sqlite3.Connection, a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    ct, df, sess, N = load_matrix(conn)
    co = pair_counts(ct)
    val, n = pmi(a, b, co, df, N)
    pmi_norm = max(0.0, val) / max(1e-9, -math.log2(1 / N)) if N > 0 else 0.0
    prof = profiles(ct, co, df, N)
    so = _cos(prof.get(a, {}), prof.get(b, {}))
    return max(pmi_norm, so)
