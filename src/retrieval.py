"""检索层：唯一的只读门面。

设计：调用方（CLI / agent / context builder）只跟这里的函数打交道，拿到的是
领域对象（Community / CardRef / CardDetail），从不直接写 clusters / cluster_members
/ cards / cards_fts 的 SQL。索引怎么建（graph / community）、tag 怎么洗
（entity_resolve）都在别处——本模块只读它们的产物。换存储 / 换算法都不影响调用方。

隐私：viewer 是 room。跨房间只给 theme + share，private 仅同房间可见；
如果一张卡没有 share，则跨房间连 theme 也不可见
（对齐 rebuild_context 的约定）。viewer=None 表示不过滤（内部 / 维护用途）。

三级展开：top_communities() → children() 往下钻 → timeline() 看某社区的时间倒序
theme 流 → card_detail() 看单卡全文。内部社区的 timeline 自动递归收集后代叶子。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from config import DEFAULT_ROOM, ROOM_SLUGS, ROOMS
from db import _jieba_seg, card_visible_clause
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
    theme: str
    room: str

    @property
    def local_time(self) -> str:
        return format_local_timestamp(self.timestamp)


@dataclass
class CardDetail:
    card_id: str
    session_id: str
    timestamp: str | None
    room: str
    theme: str
    share: str
    private: str         # 跨房间时为空（隐私过滤后）
    tags: list[str]

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
    """某社区（叶或内部）所有卡片的时间倒序 theme 流。内部社区自动递归收叶。"""
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
        SELECT DISTINCT c.card_id, c.timestamp, c.theme, c.room
        FROM cluster_members cm JOIN cards c ON c.card_id = cm.card_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.timestamp DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [CardRef(r["card_id"], r["timestamp"], r["theme"], r["room"]) for r in rows]


def card_detail(conn: sqlite3.Connection, card_id: str, viewer: str | None = None) -> CardDetail | None:
    """单卡全文。private 仅同房间可见（viewer=None 不过滤）。"""
    c = conn.execute(
        "SELECT card_id, session_id, timestamp, room, theme, share, private FROM cards WHERE card_id = ?",
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
        room=c["room"], theme=c["theme"] or "", share=c["share"] or "",
        private=private, tags=tags,
    )


def _until_bound(until: str | None) -> str | None:
    """日期-only 的 until 补到当天结束，免得 '2026-06-01' 把当天卡漏掉。"""
    if until and len(until) <= 10:
        return until + "T23:59:59Z"
    return until


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    viewer: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[CardRef]:
    """FTS 关键词检索（jieba 分词）。只搜 theme + share，跨房间安全、不泄露 private。

    since / until 是 ISO 日期或时间戳，按 cards.timestamp 过滤。
    """
    seg = _jieba_seg(query).strip()
    if not seg:
        return []
    clauses = ["cards_fts MATCH ?"]
    params: list = [f"{{theme share}} : ({seg})"]
    if since:
        clauses.append("c.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("c.timestamp <= ?")
        params.append(_until_bound(until))
    visible_clause, visible_params = card_visible_clause(viewer, "c")
    clauses.append(visible_clause)
    params.extend(visible_params)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT c.card_id, c.timestamp, c.theme, c.room
        FROM cards_fts f JOIN cards c ON c.card_id = f.card_id
        WHERE {' AND '.join(clauses)}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [CardRef(r["card_id"], r["timestamp"], r["theme"], r["room"]) for r in rows]


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
        SELECT card_id, timestamp, theme, room FROM cards
        WHERE {' AND '.join(clauses)}
        ORDER BY timestamp DESC LIMIT ?
        """,
        params,
    ).fetchall()
    return [CardRef(r["card_id"], r["timestamp"], r["theme"], r["room"]) for r in rows]


def _print_communities(rows: list[Community]) -> None:
    for c in rows:
        rng = f"{format_local_timestamp(c.t_start)[:10]}~{format_local_timestamp(c.t_end)[:10]}"
        kind = "叶" if c.is_leaf else "社区"
        print(f"📁 [{c.cluster_id}] {c.size:>3}卡 {rng} ({kind})  {c.summary[:44]}")


def _print_cards(rows: list[CardRef]) -> None:
    for r in rows:
        print(f"📄 {r.card_id}  {r.local_time}  [{r.room}] {r.theme}")


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
        print(f"== {d.card_id} ==  {d.local_time}  [{d.room}]")
        print(f"theme: {d.theme}")
        if d.share:
            print(f"\nshare: {d.share}")
        if d.private:
            print(f"\nprivate: {d.private}")
        print(f"\ntags: {' / '.join(d.tags) or '—'}")
    elif args.time:
        since = args.time[0]
        until = args.time[1] if len(args.time) > 1 else None
        _print_cards(recent(conn, since=since, until=until, limit=max(args.limit, 40),
                            viewer=args.viewer))
    elif args.query:
        hits = search(conn, args.query, limit=args.limit, viewer=args.viewer,
                      since=args.since, until=args.until)
        if not hits:
            print("（无命中）")
        _print_cards(hits)
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()
