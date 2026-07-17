"""检索层：唯一的只读门面。

设计：调用方（CLI / agent / context builder）只跟这里的函数打交道，拿到的是
领域对象（Community / CardRef / CardDetail），从不直接写 clusters / cluster_members
/ cards / cards_fts 的 SQL。索引怎么建（graph / community）、tag 怎么洗
（entity_resolve）都在别处——本模块只读它们的产物。换存储 / 换算法都不影响调用方。

隐私：viewer 是 room。跨房间只给 headline + share，private 仅同房间可见；
如果一张卡没有 share，则跨房间连 headline 也不可见
（对齐 rebuild_context 的约定）。viewer=None 表示不过滤（内部 / 维护用途）。

三级展开：top_communities() → children() 往下钻 → timeline() 看某社区的时间倒序
headline 流 → card_detail() 看单卡全文。内部社区的 timeline 自动递归收集后代叶子。

唯一的写例外（v8）：CLI 的 --card 展开会往 card_access 记一笔取用日志——列表扫过
不算，展开全文才算，treesnap 聚合成取用热喂 curator。只在 _main 里发生，库函数
仍然只读；批量脚本用 RECALL_NO_LOG=1 跳过，防止把统计灌成噪音。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from dataclasses import dataclass

from config import DEFAULT_ROOM, ROOM_SLUGS, ROOMS
from db import _jieba_query_exprs, _quote_fts, card_visible_clause
from timefmt import format_local_timestamp


@dataclass
class Community:
    cluster_id: str
    summary: str
    level: int
    is_leaf: bool
    size: int            # 后代叶子里的 primary 卡数
    t_start: str | None  # 最早卡时间（ISO）
    t_end: str | None    # 最晚卡时间（ISO）


@dataclass
class CardRef:
    card_id: str
    timestamp: str | None
    headline: str
    room: str
    why: str = ""        # 检索命中原因：上下文片段或 "tag: xxx"（timeline 等场景为空）

    @property
    def local_time(self) -> str:
        return format_local_timestamp(self.timestamp)


@dataclass
class CardDetail:
    card_id: str
    session_id: str
    timestamp: str | None
    room: str
    headline: str
    share: str
    private: str         # 跨房间时为空（隐私过滤后）
    tags: list[str]
    turn_start: int | None = None   # 原文回溯：session 内 round 区间
    turn_end: int | None = None

    @property
    def local_time(self) -> str:
        return format_local_timestamp(self.timestamp)


def _subtree_ids(conn: sqlite3.Connection, cluster_id: str) -> list[str]:
    """cluster_id 及其所有后代（含自身）。叶子返回 [自身]。"""
    rows = conn.execute(
        """
        WITH RECURSIVE sub(cluster_id) AS (
          SELECT cluster_id FROM clusters WHERE cluster_id = ?
          UNION ALL
          SELECT c.cluster_id FROM clusters c JOIN sub ON c.parent_cluster_id = sub.cluster_id
        )
        SELECT cluster_id FROM sub
        """,
        (cluster_id,),
    ).fetchall()
    return [r["cluster_id"] for r in rows]


def _viewer_room(viewer: str | None) -> str | None:
    return ROOM_SLUGS.get(viewer, viewer) if viewer else None


def _agg(conn: sqlite3.Connection, cluster_id: str) -> tuple[int, str | None, str | None]:
    """(后代叶子 primary 卡数, 最早时间, 最晚时间)。"""
    ids = _subtree_ids(conn, cluster_id)
    ph = ",".join("?" for _ in ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, MIN(c.timestamp) AS t0, MAX(c.timestamp) AS t1
        FROM cluster_members cm JOIN cards c ON c.card_id = cm.card_id
        WHERE cm.role='primary' AND cm.cluster_id IN ({ph})
        """,
        ids,
    ).fetchone()
    return row["n"], row["t0"], row["t1"]


def _is_leaf(conn: sqlite3.Connection, cluster_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM clusters WHERE parent_cluster_id = ? LIMIT 1", (cluster_id,)
    ).fetchone() is None


def _community(conn: sqlite3.Connection, row: sqlite3.Row) -> Community:
    n, t0, t1 = _agg(conn, row["cluster_id"])
    return Community(
        cluster_id=row["cluster_id"], summary=row["summary"], level=row["level"],
        is_leaf=_is_leaf(conn, row["cluster_id"]), size=n, t_start=t0, t_end=t1,
    )


def top_communities(conn: sqlite3.Connection) -> list[Community]:
    """顶层社区（level 1），按卡数降序。检索入口。"""
    rows = conn.execute(
        "SELECT cluster_id, summary, level FROM clusters WHERE level=1"
    ).fetchall()
    out = [_community(conn, r) for r in rows]
    out.sort(key=lambda c: c.size, reverse=True)
    return out


def children(conn: sqlite3.Connection, cluster_id: str) -> list[Community]:
    """直接子社区（往下钻一层）。叶社区返回 []。"""
    rows = conn.execute(
        "SELECT cluster_id, summary, level FROM clusters WHERE parent_cluster_id = ?",
        (cluster_id,),
    ).fetchall()
    out = [_community(conn, r) for r in rows]
    out.sort(key=lambda c: c.size, reverse=True)
    return out


def timeline(
    conn: sqlite3.Connection,
    cluster_id: str,
    viewer: str | None = None,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[CardRef]:
    """某社区（叶或内部）所有卡片的时间倒序 headline 流。内部社区自动递归收叶。"""
    ids = _subtree_ids(conn, cluster_id)
    ph = ",".join("?" for _ in ids)
    clauses = [f"cm.role='primary'", f"cm.cluster_id IN ({ph})"]
    params: list = list(ids)
    if since:
        clauses.append("c.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("c.timestamp <= ?")
        params.append(_until_bound(until))
    visible_clause, visible_params = card_visible_clause(viewer, "c")
    clauses.append(visible_clause)
    params.extend(visible_params)
    sql = f"""
        SELECT DISTINCT c.card_id, c.timestamp, c.headline, c.room
        FROM cluster_members cm JOIN cards c ON c.card_id = cm.card_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.timestamp DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [CardRef(r["card_id"], r["timestamp"], r["headline"], r["room"]) for r in rows]


def card_detail(conn: sqlite3.Connection, card_id: str, viewer: str | None = None) -> CardDetail | None:
    """单卡全文。private 仅同房间可见（viewer=None 不过滤）。"""
    c = conn.execute(
        "SELECT card_id, session_id, timestamp, room, headline, share, private, turn_start, turn_end"
        " FROM cards WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    if c is None:
        return None
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM card_tags WHERE card_id = ? ORDER BY tag", (card_id,)
    ).fetchall()]
    viewer_room = _viewer_room(viewer)
    if viewer_room is not None and c["room"] != viewer_room and not (c["share"] or "").strip():
        return None
    private = c["private"] or ""
    if viewer_room is not None and c["room"] != viewer_room:
        private = ""  # 跨房间：private 不可见
    return CardDetail(
        card_id=c["card_id"], session_id=c["session_id"], timestamp=c["timestamp"],
        room=c["room"], headline=c["headline"] or "", share=c["share"] or "",
        private=private, tags=tags,
        turn_start=c["turn_start"], turn_end=c["turn_end"],
    )


@dataclass
class TurnRow:
    round: int
    speaker: str
    timestamp: str | None
    text: str

    @property
    def local_time(self) -> str:
        return format_local_timestamp(self.timestamp)


def card_turns(conn: sqlite3.Connection, card_id: str, viewer: str | None = None) -> list[TurnRow] | None:
    """卡片对应的原始 turns（messages 全文），按 round/message_seq 顺序。

    原文是完整 transcript，隐私等级高于 share——只对同房间 viewer 开放
    （viewer=None 不过滤）。跨房间返回 None，与卡不存在同样处理。
    turn_start 为空的旧卡回退为整个 session。
    """
    c = conn.execute(
        "SELECT session_id, room, turn_start, turn_end FROM cards WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    if c is None:
        return None
    viewer_room = _viewer_room(viewer)
    if viewer_room is not None and c["room"] != viewer_room:
        return None
    clauses = ["t.session_id = ?"]
    params: list = [c["session_id"]]
    if c["turn_start"] is not None:
        clauses.append("t.round >= ?")
        params.append(c["turn_start"])
    if c["turn_end"] is not None:
        clauses.append("t.round <= ?")
        params.append(c["turn_end"])
    rows = conn.execute(
        f"""
        SELECT t.round, m.speaker, m.timestamp, m.text
        FROM turns t JOIN messages m ON m.source_uuid = t.source_uuid
        WHERE {' AND '.join(clauses)}
        ORDER BY t.round, t.message_seq, t.line_no
        """,
        params,
    ).fetchall()
    return [TurnRow(r["round"], r["speaker"], r["timestamp"], r["text"]) for r in rows]


def _until_bound(until: str | None) -> str | None:
    """日期-only 的 until 补到当天结束，免得 '2026-06-01' 把当天卡漏掉。"""
    if until and len(until) <= 10:
        return until + "T23:59:59Z"
    return until


_NONASCII_GAP = re.compile(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])")


def _clean_snip(snip: str) -> str:
    """FTS 表存的是 jieba 分词文本，snippet 带着词间空格；收掉 CJK 之间的空格。"""
    return _NONASCII_GAP.sub("", snip).strip()


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    viewer: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[CardRef]:
    """FTS 关键词检索（jieba 分词）＋ tag 匹配。

    多词查询两遍走：先要求全部词命中（精确），不满 limit 再按任一词命中补位
    （2026-07-15 之前只有隐式 AND，"生日礼物" 这种词组只要有一个词不在卡里就
    整卡漏掉）。每条 FTS 命中带 snippet 上下文，标在 CardRef.why 上——看得见
    是哪句话命中的，不用逐卡展开验证。

    隐私分路不变：headline+share 对所有可见卡匹配；private 只对 viewer 同房间的
    卡参与匹配，跨房间不进匹配范围、不泄露；FTS5 一条查询只允许一个 MATCH，
    两路分开查再合并。tags 走 card_tags 子串匹配，排在最后，why 标明命中的 tag
    （之前 tag 命中混在结果里看不出所以然，"生日" 拉出一堆挂着 "爷爷生日" tag
    的失眠卡还不说原因）。

    since / until 是 ISO 日期或时间戳，按 cards.timestamp 过滤。
    """
    exprs, flat_tokens = _jieba_query_exprs(query)
    if not exprs:
        return []
    and_expr = " ".join(exprs)
    # 补位一路带 * 前缀匹配：索引侧切成长词（"梧桐树"）时，查"梧桐"也能照到
    or_expr = " OR ".join(f"{_quote_fts(t)}*" for t in flat_tokens)
    viewer_room = _viewer_room(viewer)

    time_clauses: list[str] = []
    time_params: list = []
    if since:
        time_clauses.append("c.timestamp >= ?")
        time_params.append(since)
    if until:
        time_clauses.append("c.timestamp <= ?")
        time_params.append(_until_bound(until))

    def _fts(match_expr: str, extra_clauses: list[str], extra_params: list) -> list[sqlite3.Row]:
        clauses = ["cards_fts MATCH ?", *extra_clauses, *time_clauses]
        params = [match_expr, *extra_params, *time_params, limit]
        return conn.execute(
            f"""
            SELECT c.card_id, c.timestamp, c.headline, c.room, bm25(cards_fts) AS rank,
                   snippet(cards_fts, -1, '【', '】', '…', 8) AS snip
            FROM cards_fts f JOIN cards c ON c.card_id = f.card_id
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()

    visible_clause, visible_params = card_visible_clause(viewer, "c")

    def _both_routes(expr: str) -> list[sqlite3.Row]:
        # 一路：headline + share，全部可见卡；二路：private，仅同房间（viewer=None 不过滤）
        rows = _fts(f"{{headline share}} : ({expr})", [visible_clause], list(visible_params))
        if viewer_room is not None:
            rows += _fts(f"{{private}} : ({expr})", ["c.room = ?"], [viewer_room])
        else:
            rows += _fts(f"{{private}} : ({expr})", [], [])
        return rows

    def _dedupe(rows: list[sqlite3.Row], seen: set[str]) -> list[sqlite3.Row]:
        best: dict[str, tuple[float, sqlite3.Row]] = {}
        for r in rows:
            if r["card_id"] in seen:
                continue
            prev = best.get(r["card_id"])
            if prev is None or r["rank"] < prev[0]:
                best[r["card_id"]] = (r["rank"], r)
        return [r for _, r in sorted(best.values(), key=lambda t: t[0])]

    ordered = _dedupe(_both_routes(and_expr), set())
    seen = {r["card_id"] for r in ordered}
    if len(ordered) < limit:
        ordered += _dedupe(_both_routes(or_expr), seen)
        seen = {r["card_id"] for r in ordered}

    out: list[CardRef] = []
    for r in ordered[:limit]:
        snip = _clean_snip(r["snip"] or "")
        # 命中落在 headline 里时片段和主行重复，不再单列
        if snip.replace("【", "").replace("】", "").strip("…") in (r["headline"] or ""):
            snip = ""
        out.append(CardRef(r["card_id"], r["timestamp"], r["headline"], r["room"], why=snip))

    # tag 路：子串匹配（原查询串 + 长度≥2 的平铺词，含子词），排在最后
    terms = {query.strip()} | {t for t in flat_tokens if len(t) >= 2}
    terms.discard("")
    if terms and len(out) < limit:
        tag_like = " OR ".join("t.tag LIKE ?" for _ in terms)
        rows = conn.execute(
            f"""
            SELECT c.card_id, c.timestamp, c.headline, c.room,
                   group_concat(t.tag, '、') AS hit_tags
            FROM card_tags t JOIN cards c ON c.card_id = t.card_id
            WHERE ({tag_like}) AND {visible_clause}
              {'AND ' + ' AND '.join(time_clauses) if time_clauses else ''}
            GROUP BY c.card_id
            ORDER BY c.timestamp DESC
            LIMIT ?
            """,
            [f"%{t}%" for t in terms] + list(visible_params) + time_params + [limit],
        ).fetchall()
        out += [
            CardRef(r["card_id"], r["timestamp"], r["headline"], r["room"],
                    why=f"tag: {r['hit_tags']}")
            for r in rows if r["card_id"] not in seen
        ][: limit - len(out)]

    return out[:limit]


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    viewer: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[CardRef]:
    """向量语义检索（关键词失手时的第二把钥匙）。

    卡片向量一律只读 .emb_cache（Leiden 建树时算好的），缺向量的卡跳过不打 API；
    query 向量缺缓存时打一次 API（settings.embedding 的 backend/model）。

    隐私与关键词路径同一条规则：同房间卡用全文向量（含 private）；跨房间卡只用
    headline+share 向量参与匹配——全文向量含 private，不能拿去跨房间排序，否则
    排名本身就在泄露 private 的内容。share 向量由 scripts/backfill_share_vecs.py
    预热，缺的卡静默跳过。
    """
    import json

    from config import load_settings
    from embedding import _cache_key, card_text, cosine, get_embedding

    emb = load_settings().get("embedding", {})
    backend = emb.get("backend", "api")
    model = emb.get("model") or ("nomic-embed-text" if backend == "ollama" else "BAAI/bge-m3")
    qvec = get_embedding(query.strip(), backend=backend, model=model)

    clauses, params = ["1=1"], []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(_until_bound(until))
    visible_clause, visible_params = card_visible_clause(viewer, "cards")
    clauses.append(visible_clause)
    params.extend(visible_params)
    rows = conn.execute(
        f"SELECT card_id, timestamp, headline, share, private, room FROM cards WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()

    viewer_room = _viewer_room(viewer)
    scored: list[tuple[float, sqlite3.Row]] = []
    for c in rows:
        if viewer_room is None or c["room"] == viewer_room:
            text = card_text(c)
        else:
            text = "\n".join(p for p in [c["headline"] or "", c["share"] or ""] if p).strip()
        if not text:
            continue
        ck = _cache_key(backend, model, text)
        if not ck.exists():
            continue
        scored.append((cosine(qvec, json.loads(ck.read_text())), c))
    scored.sort(key=lambda t: -t[0])
    return [
        CardRef(c["card_id"], c["timestamp"], c["headline"], c["room"], why=f"语义相似 {s:.2f}")
        for s, c in scored[:limit]
    ]


def session_siblings(conn: sqlite3.Connection, card_id: str, viewer: str | None = None) -> list[CardDetail] | None:
    """一张卡所在 session 的全部卡（含自己），按 turn 顺序。重建"那天后来呢"用。

    跨房间的卡走同一套可见性规则：无 share 的兄弟卡不出现在列表里。
    卡本身不可见时返回 None（与 card_detail 一致）。
    """
    base = card_detail(conn, card_id, viewer=viewer)
    if base is None:
        return None
    visible_clause, visible_params = card_visible_clause(viewer, "cards")
    rows = conn.execute(
        f"""
        SELECT card_id FROM cards
        WHERE session_id = ? AND {visible_clause}
        ORDER BY COALESCE(turn_start, 0), timestamp
        """,
        [base.session_id, *visible_params],
    ).fetchall()
    return [d for r in rows if (d := card_detail(conn, r["card_id"], viewer=viewer))]


def wander(conn: sqlite3.Connection, limit: int = 3, viewer: str | None = None) -> list[CardRef]:
    """随手翻：抽几张翻动最少的旧卡（七天以上，偏向从没被展开过的）。

    不是检索是闲逛——search/--sem 是"我要想起某件事"，这个是"让某件事自己
    想起我"。同等冷度里随机，翻过的次数写在 why 上。取用热度只在真展开时记
    （CLI --card / --expand），wander 列表本身不记，扫过不算翻过。
    """
    visible_clause, visible_params = card_visible_clause(viewer, "c")
    rows = conn.execute(
        f"""
        SELECT c.card_id, c.timestamp, c.headline, c.room, COALESCE(a.n, 0) AS n_access
        FROM cards c
        LEFT JOIN (SELECT card_id, COUNT(*) AS n FROM card_access GROUP BY card_id) a
          ON a.card_id = c.card_id
        WHERE {visible_clause} AND c.timestamp < datetime('now', '-7 days')
        ORDER BY n_access ASC, RANDOM()
        LIMIT ?
        """,
        [*visible_params, limit],
    ).fetchall()
    return [
        CardRef(r["card_id"], r["timestamp"], r["headline"], r["room"],
                why="还没翻过" if r["n_access"] == 0 else f"翻过 {r['n_access']} 次")
        for r in rows
    ]


def recent(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    limit: int = 40,
    viewer: str | None = None,
) -> list[CardRef]:
    """按时间范围列卡（不分社区），时间倒序。since/until 为 ISO 日期或时间戳。"""
    clauses, params = ["1=1"], []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(_until_bound(until))
    visible_clause, visible_params = card_visible_clause(viewer, "cards")
    clauses.append(visible_clause)
    params.extend(visible_params)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT card_id, timestamp, headline, room FROM cards
        WHERE {' AND '.join(clauses)}
        ORDER BY timestamp DESC LIMIT ?
        """,
        params,
    ).fetchall()
    return [CardRef(r["card_id"], r["timestamp"], r["headline"], r["room"]) for r in rows]


def _print_communities(rows: list[Community]) -> None:
    for c in rows:
        rng = f"{format_local_timestamp(c.t_start)[:10]}~{format_local_timestamp(c.t_end)[:10]}"
        kind = "叶" if c.is_leaf else "社区"
        print(f"📁 [{c.cluster_id}] {c.size:>3}卡 {rng} ({kind})  {c.summary[:44]}")


def _print_cards(rows: list[CardRef]) -> None:
    for r in rows:
        print(f"📄 {r.card_id}  {r.local_time}  [{r.room}] {r.headline}")
        if r.why:
            print(f"    ⌙ {r.why}")


def _log_access(conn: sqlite3.Connection, card_id: str, viewer: str | None) -> None:
    if os.environ.get("RECALL_NO_LOG") == "1":
        return
    conn.execute(
        "INSERT INTO card_access (card_id, viewer, source, ts) VALUES (?, ?, 'search', ?)",
        (card_id, viewer or "", dt.datetime.now(dt.UTC).isoformat()),
    )
    conn.commit()


def _print_card_detail(conn: sqlite3.Connection, d: CardDetail, viewer: str | None, show_turns: bool) -> None:
    print(f"== {d.card_id} ==  {d.local_time}  [{d.room}]")
    print(f"headline: {d.headline}")
    if d.share:
        print(f"\nshare: {d.share}")
    if d.private:
        print(f"\nprivate: {d.private}")
    print(f"\ntags: {' / '.join(d.tags) or '—'}")
    if show_turns:
        rows = card_turns(conn, d.card_id, viewer=viewer)
        if rows is None:
            print("\n（原文仅同房间可见）")
        elif not rows:
            print("\n（原文 turns 未入库）")
        else:
            print(f"\n── 原文 R{rows[0].round}–R{rows[-1].round} ──")
            for t in rows:
                print(f"\n[{t.speaker}] {t.local_time}")
                print(t.text)
    elif d.turn_start is not None:
        print(f"原文: 加 --turns 展开（R{d.turn_start}–R{d.turn_end}）")


def _main() -> None:
    import argparse
    from db import connect

    ap = argparse.ArgumentParser(description="recall 检索（v2 Leiden 索引）")
    ap.add_argument("query", nargs="?", help="关键词搜索（FTS）")
    ap.add_argument("--viewer", default=DEFAULT_ROOM, help=f"房间视角 {'|'.join(ROOMS)}（private 仅同房间可见）")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--top", action="store_true", help="列顶层社区")
    ap.add_argument("--cluster", metavar="ID", help="展开一个社区的时间线（含子社区时列子社区）")
    ap.add_argument("--card", metavar="ID", help="展开一张卡的全文")
    ap.add_argument("--turns", action="store_true", help="配合 --card：附上原始对话全文（仅同房间的卡）")
    ap.add_argument("--around", action="store_true", help="配合 --card：列同 session 的前后卡片")
    ap.add_argument("--sem", action="store_true", help="语义检索（向量相似度），关键词失手时用")
    ap.add_argument("--wander", nargs="?", const=3, type=int, metavar="N",
                    help="随手翻 N 张冷卡（默认 3，偏向没被翻过的旧卡）")
    ap.add_argument("--expand", type=int, metavar="N", help="搜索后自动展开前 N 条命中的全文（计取用）")
    ap.add_argument("--time", nargs="+", metavar="DATE", help="时间范围 START [END]")
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args()
    conn = connect()

    if args.top:
        _print_communities(top_communities(conn))
    elif args.cluster:
        subs = children(conn, args.cluster)
        if subs:
            print("── 子社区 ──")
            _print_communities(subs)
            print("\n── 全部卡片（时间倒序）──")
        _print_cards(timeline(conn, args.cluster, viewer=args.viewer,
                              limit=args.limit, since=args.since, until=args.until))
    elif args.card:
        d = card_detail(conn, args.card, viewer=args.viewer)
        if not d:
            print(f"not found: {args.card}")
            return
        _log_access(conn, d.card_id, args.viewer)
        _print_card_detail(conn, d, args.viewer, args.turns)
        if args.around:
            sibs = session_siblings(conn, args.card, viewer=args.viewer) or []
            print(f"\n── 同 session（{len(sibs)} 张）──")
            for s in sibs:
                mark = "→" if s.card_id == d.card_id else " "
                rng = f"R{s.turn_start}–R{s.turn_end}" if s.turn_start is not None else "R?"
                print(f"{mark} 📄 {s.card_id}  {s.local_time}  {rng}  {s.headline}")
    elif args.wander is not None:
        _print_cards(wander(conn, limit=args.wander, viewer=args.viewer))
    elif args.time:
        since = args.time[0]
        until = args.time[1] if len(args.time) > 1 else None
        _print_cards(recent(conn, since=since, until=until, limit=max(args.limit, 40),
                            viewer=args.viewer))
    elif args.query:
        if args.sem:
            hits = semantic_search(conn, args.query, limit=args.limit, viewer=args.viewer,
                                   since=args.since, until=args.until)
        else:
            hits = search(conn, args.query, limit=args.limit, viewer=args.viewer,
                          since=args.since, until=args.until)
        if not hits:
            print("（无命中）" + ("" if args.sem else "；试试 --sem 语义检索"))
        _print_cards(hits)
        for ref in hits[: args.expand or 0]:
            d = card_detail(conn, ref.card_id, viewer=args.viewer)
            if d is None:
                continue
            _log_access(conn, d.card_id, args.viewer)
            print()
            _print_card_detail(conn, d, args.viewer, show_turns=False)
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()
