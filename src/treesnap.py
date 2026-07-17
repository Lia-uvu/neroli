"""Midlayer 模块（一）：夜间树快照 + 叶子层身份匹配 + 新卡驱动的树变化报告 + heat 渲染。

纯 SQL + 集合运算，不需要模型。设计见 docs/proposal-20260702-midlayer-tree-privacy.md
「未决 1 / 未决 2」。

职责边界：本模块只读 clusters / cluster_members / cards / card_tags / tag_entity_map /
entities，写自己的 tree_snapshots / tree_snapshot_members；只依赖 db / config / timefmt。
不 import Index（graph/community/embedding）——社区标签自带一条带可见性过滤的实体聚合
SQL（宁可重复一小段，不跨模块 import，见 ARCHITECTURE.md 边界规则）。

每晚 rebuild-index 之后 snapshot() 把当前树原样拷成「今夜」。lineage() 对昨夜/今夜的
叶子做 containment 匹配（overlap(O→N)=|O∩N|/|O|，primary 成员），给每个今夜叶归因
来源线。render_report() 以**新卡驱动**拼《树变化报告》喂给夜间 curator——只报今晚有新卡
（默认晚于上次快照夜；curator 改用上次成功 digest 夜）的叶子，lineage 只作尾注身份线索，
不定义「新」。
diff() 保留供 dry-run 的拓扑统计（new/continued/merged/split），报告不再用它。
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from config import load_settings, stop_tags
from db import card_visible_clause
from timefmt import LOCAL_TZ, format_local_timestamp

# containment 阈值（proposal §未决1 拍脑袋初值，靠 dry-run 调；settings.midlayer 可调）。
_ml = load_settings().get("midlayer", {})
HOME_MIN = _ml.get("home_min", 0.5)    # O 的 argmax 归宿 < 此值视为「打散」，无归宿
SPLIT_MIN = _ml.get("split_min", 0.3)  # 一个 O 的成员分到 ≥2 个 N、各占 O 的 ≥此比例 = split

# 标签聚合前过滤的背景词。词表统一住 config（含称呼别名），不用跨模块 import embedding。
_STOP = stop_tags()


def tonight_night() -> str:
    """今夜的本地日期字符串（如 2026-07-03）。night 一律用本地时区。"""
    return dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


# ---- 快照 ----

def snapshot(conn: sqlite3.Connection, night: str | None = None,
             keep_nights: int | None = None) -> dict:
    """把当前 clusters / cluster_members 原样拷成 night 的快照，并清理过期夜。

    同夜重跑幂等（先删该夜再拷）。keep_nights 缺省读 settings.midlayer.snapshot_keep_nights。
    """
    night = night or tonight_night()
    if keep_nights is None:
        keep_nights = load_settings().get("midlayer", {}).get("snapshot_keep_nights", 14)

    conn.execute("DELETE FROM tree_snapshot_members WHERE night = ?", (night,))
    conn.execute("DELETE FROM tree_snapshots WHERE night = ?", (night,))
    conn.execute(
        """
        INSERT INTO tree_snapshots (night, cluster_id, parent_cluster_id, level, summary)
        SELECT ?, cluster_id, parent_cluster_id, level, summary FROM clusters
        """,
        (night,),
    )
    conn.execute(
        """
        INSERT INTO tree_snapshot_members (night, cluster_id, card_id, role)
        SELECT ?, cluster_id, card_id, role FROM cluster_members
        """,
        (night,),
    )
    pruned = _prune_snapshots(conn, keep_nights)
    conn.commit()
    n_clusters = conn.execute(
        "SELECT COUNT(*) AS n FROM tree_snapshots WHERE night = ?", (night,)
    ).fetchone()["n"]
    n_members = conn.execute(
        "SELECT COUNT(*) AS n FROM tree_snapshot_members WHERE night = ? AND role='primary'",
        (night,),
    ).fetchone()["n"]
    return {"night": night, "clusters": n_clusters, "primary_members": n_members,
            "pruned_nights": pruned}


def _prune_snapshots(conn: sqlite3.Connection, keep_nights: int) -> list[str]:
    nights = snapshot_nights(conn)
    stale = nights[keep_nights:] if keep_nights and keep_nights > 0 else []
    for n in stale:
        conn.execute("DELETE FROM tree_snapshot_members WHERE night = ?", (n,))
        conn.execute("DELETE FROM tree_snapshots WHERE night = ?", (n,))
    return stale


def snapshot_nights(conn: sqlite3.Connection) -> list[str]:
    """已有快照的夜，新到旧。"""
    return [r["night"] for r in conn.execute(
        "SELECT DISTINCT night FROM tree_snapshots ORDER BY night DESC"
    ).fetchall()]


def prev_night(conn: sqlite3.Connection, night: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(night) AS p FROM tree_snapshots WHERE night < ?", (night,)
    ).fetchone()
    return row["p"] if row and row["p"] else None


# ---- 叶子层身份匹配 ----

def _leaf_members(conn: sqlite3.Connection, night: str) -> dict[str, set[str]]:
    """该夜每个叶子（快照内无子）的 primary 成员集合。"""
    leaves = {
        r["cluster_id"] for r in conn.execute(
            """
            SELECT cluster_id FROM tree_snapshots s
            WHERE night = ? AND NOT EXISTS (
              SELECT 1 FROM tree_snapshots c
              WHERE c.night = s.night AND c.parent_cluster_id = s.cluster_id
            )
            """,
            (night,),
        ).fetchall()
    }
    out: dict[str, set[str]] = {cid: set() for cid in leaves}
    for r in conn.execute(
        "SELECT cluster_id, card_id FROM tree_snapshot_members WHERE night = ? AND role='primary'",
        (night,),
    ).fetchall():
        if r["cluster_id"] in out:
            out[r["cluster_id"]].add(r["card_id"])
    return {cid: members for cid, members in out.items() if members}


def diff(conn: sqlite3.Connection, night: str, prev: str) -> dict:
    """昨夜(prev) → 今夜(night) 的叶子层事件。

    overlap(O→N) = |O∩N| / |O|（primary 成员，containment 抗增长）。
    - new       : 今夜叶 N 没有任何昨夜叶 O 主要落入（无 O 以 N 为归宿）
    - continued : 一对一，附新增卡 id
    - merged    : ≥2 个昨夜叶 O 主要落入同一今夜叶 N
    - split     : 一个昨夜叶 O 的成员分到 ≥2 个今夜叶 N（各占 O 的 ≥SPLIT_MIN）
    """
    old = _leaf_members(conn, prev)
    new = _leaf_members(conn, night)

    home: dict[str, str | None] = {}          # O -> 归宿 N（或 None 打散）
    split_targets: dict[str, list[str]] = {}   # O -> 各占 ≥SPLIT_MIN 的 N 列表
    for o, o_members in old.items():
        best_n, best_ov = None, 0.0
        targets = []
        for n, n_members in new.items():
            inter = len(o_members & n_members)
            if not inter:
                continue
            ov = inter / len(o_members)
            if ov > best_ov:
                best_n, best_ov = n, ov
            if ov >= SPLIT_MIN:
                targets.append(n)
        home[o] = best_n if best_ov >= HOME_MIN else None
        if len(targets) >= 2:
            split_targets[o] = sorted(targets)

    homes_by_n: dict[str, list[str]] = {}
    for o, n in home.items():
        if n is not None:
            homes_by_n.setdefault(n, []).append(o)

    events: dict[str, list] = {"new": [], "continued": [], "merged": [], "split": []}
    for n, n_members in new.items():
        os = sorted(homes_by_n.get(n, []))
        if not os:
            events["new"].append({"cluster_id": n, "members": sorted(n_members)})
        elif len(os) == 1:
            o = os[0]
            added = sorted(n_members - old[o])
            events["continued"].append({"cluster_id": n, "from": o, "added": added})
        else:
            events["merged"].append({"cluster_id": n, "from": os})
    for o, targets in split_targets.items():
        events["split"].append({"cluster_id": o, "into": targets})
    return events


def lineage(conn: sqlite3.Connection, night: str, prev: str) -> dict[str, list[tuple[str, float]]]:
    """每个今夜叶 N 的来源线：[(昨夜叶 O, 占比 |O∩N|/|O|), ...]，占比降序。空=新线。

    只做身份归因，**不定义「新」**——一条线新不新由 render_report 按 fresh 卡判定，
    这里的占比只是给 curator 对上 prev-digest 里旧线的线索（不再靠 HOME_MIN 二分）。
    """
    old = _leaf_members(conn, prev)
    new = _leaf_members(conn, night)
    out: dict[str, list[tuple[str, float]]] = {}
    for n, n_members in new.items():
        sources = [
            (o, len(o_members & n_members) / len(o_members))
            for o, o_members in old.items()
            if o_members & n_members
        ]
        sources.sort(key=lambda t: t[1], reverse=True)
        out[n] = sources
    return out


def _fresh_cutoff(prev: str) -> str:
    """fresh 分界：prev 快照夜的次日本地零点，转 UTC ISO。

    晚于此的卡 = 「上次快照以来的新卡」。用夜界而非硬 24h，跳夜补跑时不漏新卡。
    """
    boundary = dt.datetime.strptime(prev, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ) + dt.timedelta(days=1)
    return boundary.astimezone(dt.UTC).isoformat()


# ---- heat + 标签 ----

def _subtree_card_stats(conn: sqlite3.Connection, night: str, cluster_id: str,
                        viewer: str | None) -> tuple[list[str], int, str]:
    """cluster_id 子树里 viewer 可见的 primary 卡：(card_ids, n_24h, 最后活跃本地日期)。

    卡时间取主库 cards.timestamp（快照只存归属，不存时间）。
    最后活跃给绝对日期而非 7d 窗口计数：冷要有深浅，curator 才知道 digest 里哪段该衰减。
    """
    visible, vparams = card_visible_clause(viewer, "c")
    rows = conn.execute(
        f"""
        WITH RECURSIVE sub(cluster_id) AS (
          SELECT cluster_id FROM tree_snapshots WHERE night = ? AND cluster_id = ?
          UNION ALL
          SELECT s.cluster_id FROM tree_snapshots s
          JOIN sub ON s.parent_cluster_id = sub.cluster_id
          WHERE s.night = ?
        )
        SELECT DISTINCT c.card_id, c.timestamp
        FROM tree_snapshot_members m
        JOIN cards c ON c.card_id = m.card_id
        WHERE m.night = ? AND m.role = 'primary'
          AND m.cluster_id IN (SELECT cluster_id FROM sub)
          AND {visible}
        """,
        (night, cluster_id, night, night, *vparams),
    ).fetchall()
    now = dt.datetime.now(dt.UTC)
    c24 = (now - dt.timedelta(hours=24)).isoformat()
    card_ids = [r["card_id"] for r in rows]
    n24 = sum(1 for r in rows if r["timestamp"] and r["timestamp"] >= c24)
    latest = max((r["timestamp"] for r in rows if r["timestamp"]), default=None)
    return card_ids, n24, _local_date(latest)


def _local_date(ts: str | None) -> str:
    """ISO 时间戳 → 本地日期（YYYY-MM-DD）；无时间戳给「—」。"""
    if not ts:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except ValueError:
        return ts[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def _access_by_card(conn: sqlite3.Connection, since: str) -> dict[str, tuple[int, dict[str, int]]]:
    """card_access 里 since 之后的取用计数（只认 source='search'）：card_id → (总次数, {viewer: 次数})。

    取用热的原始事实层。表可能尚未建（旧库没跑 008），容错返回空。
    """
    try:
        rows = conn.execute(
            "SELECT card_id, viewer, COUNT(*) AS n FROM card_access "
            "WHERE source = 'search' AND ts >= ? GROUP BY card_id, viewer",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, tuple[int, dict[str, int]]] = {}
    for r in rows:
        total, by = out.get(r["card_id"], (0, {}))
        by[r["viewer"] or "?"] = r["n"]
        out[r["card_id"]] = (total + r["n"], by)
    return out


def _visible_label(conn: sqlite3.Connection, card_ids: list[str], topn: int = 4) -> str:
    """从给定（已按 viewer 过滤的）卡的实体重新聚合社区标签。~15 行本地 SQL。"""
    if not card_ids:
        return "(空)"
    ph = ",".join("?" for _ in card_ids)
    stop = sorted(_STOP)
    stop_ph = ",".join("?" for _ in stop)
    rows = conn.execute(
        f"""
        SELECT e.canonical_name AS ent, COUNT(*) AS c
        FROM card_tags ct
        JOIN tag_entity_map map ON map.tag = ct.tag
        JOIN entities e ON e.entity_id = map.entity_id
        WHERE ct.card_id IN ({ph})
          AND lower(e.canonical_name) NOT IN ({stop_ph})
        GROUP BY e.entity_id ORDER BY c DESC LIMIT ?
        """,
        (*card_ids, *stop, topn),
    ).fetchall()
    return " ".join(f"{r['ent']}({r['c']})" for r in rows) or "(无实体)"


def _card_themes(conn: sqlite3.Connection, card_ids: list[str], limit: int = 3) -> list[str]:
    """取若干卡的 theme 作样例，**保持传入顺序**（调用方已按时间倒序、按 viewer 过滤）。"""
    if not card_ids:
        return []
    sample = card_ids[:limit]
    ph = ",".join("?" for _ in sample)
    rows = conn.execute(
        f"SELECT card_id, theme FROM cards WHERE card_id IN ({ph})", sample
    ).fetchall()
    by = {r["card_id"]: r["theme"] for r in rows}
    return [by[cid] for cid in sample if by.get(cid)]


# ---- 报告 ----

def render_report(conn: sqlite3.Connection, night: str | None = None,
                  viewer: str | None = None,
                  fresh_after_night: str | None = None,
                  fresh_from_success: bool = False) -> str:
    """《树变化报告》：顶层社区 heat + 昨夜/今夜叶子事件。条目式，喂模型用。

    viewer 过滤：只统计/展示可见卡，标签从可见卡重聚合，整卡不可见的社区整行不出现。
    首夜无昨夜快照时标「基线夜」，只出 heat。
    """
    night = night or tonight_night()
    if not conn.execute(
        "SELECT 1 FROM tree_snapshots WHERE night = ? LIMIT 1", (night,)
    ).fetchone():
        return f"（无 {night} 的树快照，先跑 --curate-snapshot）"

    prev = prev_night(conn, night)
    # fresh 默认以上次快照为界；curator 可传最近一次成功 digest 夜，确保失败夜的新卡
    # 不会因为快照已经推进而永久漏掉。
    fresh_baseline = fresh_after_night if fresh_from_success else (fresh_after_night or prev)
    acc_since = _fresh_cutoff(fresh_baseline) if fresh_baseline else (
        dt.datetime.now(dt.UTC) - dt.timedelta(days=7)).isoformat()
    acc = _access_by_card(conn, acc_since)
    header = f"== 树变化 {night}"
    header += f"（对比 {prev}）==" if prev else "（基线夜，无对比）=="
    lines = [header, "", "顶层社区（卡数 / 24h新卡 / 最后活跃 / ↻取用）："]

    tops = conn.execute(
        "SELECT cluster_id FROM tree_snapshots WHERE night = ? AND level = 1", (night,)
    ).fetchall()
    top_stats = []
    for r in tops:
        cid = r["cluster_id"]
        card_ids, n24, last_active = _subtree_card_stats(conn, night, cid, viewer)
        if not card_ids:
            continue  # 整社区不可见：整行不出现
        n_read = sum(acc[c][0] for c in card_ids if c in acc)
        top_stats.append((cid, len(card_ids), n24, last_active, n_read, _visible_label(conn, card_ids)))
    top_stats.sort(key=lambda t: t[1], reverse=True)
    for cid, total, n24, last_active, n_read, label in top_stats:
        read_tag = f"  ↻{n_read}" if n_read else ""
        lines.append(f"  {cid} {label}   {total}卡  +{n24}/24h  最后活跃{last_active}{read_tag}")

    if not prev:
        lines.append("")
        lines.append("事件：基线夜，无昨夜对比。")
        return "\n".join(lines) + "\n"

    # 新卡驱动：事件 = 今晚有新卡（timestamp 晚于上次快照夜）的叶子，按新卡数排序。
    # 拓扑无权定义「新」——lineage 只在行尾注记承接自昨夜哪条线，供 curator 对上 prev-digest。
    lin = lineage(conn, night, prev)
    leaves = _leaf_members(conn, night)
    cutoff = (
        _fresh_cutoff(fresh_baseline)
        if fresh_baseline
        else "0001-01-01T00:00:00+00:00"
    )
    fresh_by_n = {n: _fresh_visible_cards(conn, sorted(m), viewer, cutoff)
                  for n, m in leaves.items()}

    lines.append("")
    basis = "上次成功整理以来" if fresh_from_success else "上次快照以来"
    lines.append(f"今晚有新卡的线（新={basis}，按新卡数排序）：")

    rows = []
    for n, fresh in fresh_by_n.items():
        if not fresh:
            continue
        all_vis = _visible_cards(conn, sorted(leaves[n]), viewer)
        rows.append((
            len(fresh), n, _visible_label(conn, all_vis), len(all_vis),
            _lineage_note(lin.get(n, [])),
            "；".join(_card_themes(conn, [cid for cid, _ in fresh], limit=2)),
        ))
    rows.sort(key=lambda r: r[0], reverse=True)
    for cnt, n, label, total, note, themes in rows:
        lines.append(f"  {n} [{label}]  +{cnt}卡（共{total}卡）  {note}")
        if themes:
            lines.append(f"        新卡样例：{themes}")
    if not rows:
        lines.append("  （无新卡）")

    # 取用热：上次快照以来被 --card 展开过的可见卡。写入冷但取用热的线也算「有动静」——
    # 一条线没有新卡不代表凉了，可能正被反复拿起（如 2026-07-09 的改名夜，教训）。
    if acc:
        vis = set(_visible_cards(conn, sorted(acc), viewer))
        acc_rows = sorted(((acc[c][0], c) for c in vis), reverse=True)[:8]
        if acc_rows:
            lines.append("")
            lines.append("本期被重新取用的卡（↻次数，含哪些房间在翻）：")
            for n_read, c in acc_rows:
                by = "、".join(f"{room}×{n}" for room, n in sorted(acc[c][1].items()))
                theme = (_card_themes(conn, [c], limit=1) or ["—"])[0]
                lines.append(f"  {c} ↻{n_read}（{by}）  {theme[:48]}")

    reshuffle = _reshuffle_count(lin, fresh_by_n)
    if reshuffle:
        lines.append(f"另有拓扑重排 {reshuffle} 处（无新卡，略）")
    return "\n".join(lines) + "\n"


def _visible_cards(conn: sqlite3.Connection, card_ids: list[str],
                   viewer: str | None) -> list[str]:
    """从 card_ids 里筛出 viewer 可见的，保序。"""
    if not card_ids or viewer is None:
        return list(card_ids)
    ph = ",".join("?" for _ in card_ids)
    visible, vparams = card_visible_clause(viewer, "c")
    rows = conn.execute(
        f"SELECT card_id FROM cards c WHERE c.card_id IN ({ph}) AND {visible}",
        (*card_ids, *vparams),
    ).fetchall()
    keep = {r["card_id"] for r in rows}
    return [cid for cid in card_ids if cid in keep]


def _fresh_visible_cards(conn: sqlite3.Connection, card_ids: list[str],
                         viewer: str | None, cutoff: str) -> list[tuple[str, str]]:
    """card_ids 里 viewer 可见且 timestamp >= cutoff 的卡，时间倒序返回 [(card_id, ts)]。"""
    if not card_ids:
        return []
    ph = ",".join("?" for _ in card_ids)
    visible, vparams = card_visible_clause(viewer, "c")
    rows = conn.execute(
        f"""
        SELECT card_id, timestamp FROM cards c
        WHERE c.card_id IN ({ph}) AND {visible} AND c.timestamp >= ?
        ORDER BY c.timestamp DESC
        """,
        (*card_ids, *vparams, cutoff),
    ).fetchall()
    return [(r["card_id"], r["timestamp"]) for r in rows]


def _lineage_note(sources: list[tuple[str, float]]) -> str:
    """行尾承接注记：无来源=新线；单来源=延续；多个**实质**来源=并自（列前两条，附占比）。

    只有占比 ≥SPLIT_MIN 的来源算实质合并；占比零头的尾巴当噪声忽略（否则一条本质是
    延续的线会被误标成「并自」）。全是零头时保留占比最高的一条当延续来源。
    """
    if not sources:
        return "←新线"
    sig = [(o, p) for o, p in sources if p >= SPLIT_MIN] or sources[:1]
    parts = [f"{o}({p:.0%})" for o, p in sig[:2]]
    if len(sig) == 1:
        return f"←延续昨夜{parts[0]}"
    return "←并自昨夜" + "+".join(parts)


def _reshuffle_count(lin: dict[str, list[tuple[str, float]]],
                     fresh_by_n: dict[str, list]) -> int:
    """无新卡但结构变了的处数（新线 / 合并 / 打散），仅作报告末尾一行提示。

    per-N 看新线(0 来源)与合并(≥2 个占比 ≥SPLIT_MIN 的实质来源)；再从来源反查打散
    （一个昨夜叶散到 ≥2 个今夜叶、各占 ≥SPLIT_MIN 且去向全无新卡）。占比零头的尾巴
    不算合并，免得把本质是延续的线也计进去。粗计即可，只为「树动过但没新东西」的提示。
    """
    stale = {n for n, fresh in fresh_by_n.items() if not fresh}
    count = sum(
        1 for n in stale
        if not lin.get(n) or sum(1 for _, p in lin[n] if p >= SPLIT_MIN) >= 2
    )
    fanned: dict[str, set[str]] = {}
    for n, sources in lin.items():
        for o, p in sources:
            if p >= SPLIT_MIN:
                fanned.setdefault(o, set()).add(n)
    count += sum(1 for ns in fanned.values() if len(ns) >= 2 and ns <= stale)
    return count


def _main() -> None:
    import argparse
    from db import connect

    ap = argparse.ArgumentParser(description="treesnap：夜间树快照 / diff 报告")
    ap.add_argument("--snapshot", action="store_true", help="拷今夜快照并清理过期夜")
    ap.add_argument("--report", action="store_true", help="打印《树变化报告》")
    ap.add_argument("--night", default=None, help="指定夜（默认今夜本地日期）")
    ap.add_argument("--viewer", default=None, help="房间视角（rooms.json 中的房间名，默认全量）")
    args = ap.parse_args()
    conn = connect()
    if args.snapshot:
        print(snapshot(conn, night=args.night))
    if args.report or not args.snapshot:
        print(render_report(conn, night=args.night, viewer=args.viewer))


if __name__ == "__main__":
    _main()
