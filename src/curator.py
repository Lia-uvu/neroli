"""Midlayer 模块（三）：夜间 curator 编排。

工作台导出 + 夜间 curator 调用：每房间导出过滤后的工作台，再让该房间 agent
根据当前记忆树重写 digest，并维护 constants。

工作台（每晚每房间导出到 data/curator/<night>/<room>/）：
  view.db        viewer 过滤的库拷贝：不可见的卡整行不进来，他房 shared 卡 private 置空
  tree-report.md 该 viewer 的《树变化报告》（treesnap 产物）
  tree-full-picture.md 「各主线的来路」：子簇首末卡弧线图，digest 的骨架（{tree-full-picture}）
  constants.json 现有篮子（shared 全部 + 本房 private active）
  recall         检索 wrapper，指向本目录 view.db，复用 retrieval
  submit         提交门 wrapper：审 token 预算/字段/constant_id，过了才落 submission.json
                 （校验逻辑在 submitcheck.py，run_curation 读结果时用同一份复验）

隔离靠「工作台里只有过滤后的数据」——不依赖模型自觉（proposal §未决3）。

边界：只依赖 db / config / timefmt / treesnap（同模块）；不 import 其他五模块。
recall wrapper 是导出到工作台的独立脚本，在工作台内 import Search 门面（retrieval），
属工具面而非本模块的跨模块 import。
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import re
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Callable

import submitcheck
import treesnap
from config import MEMORY, ROOM_DIRS, ROOM_SLUGS, ROOMS, fill_username, load_settings
from db import DB, card_visible_clause, create_pipeline_run, record_model_call
from model import ModelRunner, model_attempts

CURATOR_DIR = MEMORY / "data" / "curator"
SRC_DIR = MEMORY / "src"
# agent 本人维护自己的记忆（早期方案是第三方馆员代管，已弃用）
PROMPT_FILE = MEMORY / "prompts" / "night-curator.md"

# view.db 需要的表（检索用），照抄 schema.sql 的定义，不含中期层/审计/原始层。
_VIEW_SCHEMA = """
CREATE TABLE cards (
  card_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_start INTEGER, turn_end INTEGER,
  headline TEXT NOT NULL DEFAULT '', share TEXT NOT NULL DEFAULT '', private TEXT NOT NULL DEFAULT '',
  timestamp TEXT, room TEXT NOT NULL, model TEXT
);
CREATE INDEX cards_time_idx ON cards(timestamp);
CREATE TABLE card_tags (card_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (card_id, tag));
CREATE INDEX card_tags_tag_idx ON card_tags(tag);
CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE, name_embedding BLOB, created_at TEXT);
CREATE TABLE tag_entity_map (tag TEXT PRIMARY KEY, entity_id INTEGER NOT NULL);
CREATE INDEX tag_entity_map_entity_idx ON tag_entity_map(entity_id);
CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', parent_cluster_id TEXT, level INTEGER NOT NULL DEFAULT 0);
CREATE INDEX clusters_parent_idx ON clusters(parent_cluster_id);
CREATE TABLE cluster_members (card_id TEXT NOT NULL, cluster_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'primary', PRIMARY KEY (card_id, cluster_id));
CREATE INDEX cluster_members_cluster_idx ON cluster_members(cluster_id);
CREATE VIRTUAL TABLE cards_fts USING fts5(card_id UNINDEXED, headline, share, private);
"""

_RECALL_TEMPLATE = '''#!/usr/bin/env python3
"""工作台检索 wrapper：指向本目录 view.db，复用仓库 retrieval。

view.db 已按房间过滤过（不可见卡不在库里、他房 private 已置空），所以 viewer=None——
无需再过滤。子命令：search（默认，--expand N 直接展开前 N 条命中）/ --top /
--cluster ID / --card ID [--around 列同 session 卡] [--before N|all] / --time START [END]。
"""
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, {src!r})
_VENDOR = str(Path({src!r}).resolve().parent / "vendor")
if Path(_VENDOR).exists() and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
# 工作台常被 agentic CLI 放进干净 Python 环境；先借仓库 vendor 的 jieba，
# 真没有时仍应允许 --cluster/--card/--time 这类下钻运行，关键词搜索退化成粗分词。
try:
    import jieba  # noqa: F401
except ModuleNotFoundError:
    import re

    jieba = types.ModuleType("jieba")
    _cut = lambda text, **_kw: re.findall(r"[A-Za-z0-9_.-]+|[\\u4e00-\\u9fff]", text)
    jieba.cut = _cut
    jieba.lcut = lambda text, **kw: list(_cut(text, **kw))
    jieba.cut_for_search = _cut
    sys.modules["jieba"] = jieba

import retrieval  # noqa: E402

DB = Path(__file__).resolve().parent / "view.db"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _detail(d):
    print(f"== {{d.card_id}} ==  {{d.local_time}}  [{{d.room}}]")
    print(f"headline: {{d.headline}}")
    if d.share:
        print(f"\\nshare: {{d.share}}")
    if d.private:
        print(f"\\nprivate: {{d.private}}")
    print(f"\\ntags: {{' / '.join(d.tags) or '—'}}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="工作台检索（view.db，已按房间过滤）")
    ap.add_argument("query", nargs="?", help="关键词搜索（FTS）")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--top", action="store_true", help="列顶层社区")
    ap.add_argument("--cluster", metavar="ID", help="展开一个社区的时间线")
    ap.add_argument("--card", metavar="ID", help="展开一张卡的全文")
    ap.add_argument("--around", action="store_true", help="配合 --card：列同 session 的前后卡片")
    ap.add_argument("--before", nargs="?", const="all", metavar="N|all",
                    help="配合 --card：列同 session 此前 N 张卡；省略 N 或写 all 列全部")
    ap.add_argument("--expand", type=int, metavar="N", help="搜索或 --before 后自动展开前 N 张全文")
    ap.add_argument("--time", nargs="+", metavar="DATE", help="时间范围 START [END]")
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args()
    before_limit = None
    if args.before is not None and args.before != "all":
        try:
            before_limit = int(args.before)
        except ValueError:
            ap.error("--before 需要正整数或 all")
        if before_limit < 1:
            ap.error("--before 需要正整数或 all")
    if args.before is not None and not args.card:
        ap.error("--before 需要配合 --card ID")
    conn = _conn()
    if args.top:
        retrieval._print_communities(retrieval.top_communities(conn))
    elif args.cluster:
        subs = retrieval.children(conn, args.cluster)
        if subs:
            print("── 子社区 ──")
            retrieval._print_communities(subs)
            print("\\n── 全部卡片（时间倒序）──")
        retrieval._print_cards(retrieval.timeline(conn, args.cluster, limit=args.limit,
                               since=args.since, until=args.until))
    elif args.card:
        d = retrieval.card_detail(conn, args.card)
        if not d:
            print(f"not found: {{args.card}}")
            return
        _detail(d)
        if args.around:
            sibs = retrieval.session_siblings(conn, args.card) or []
            print(f"\\n── 同 session（{{len(sibs)}} 张）──")
            for s in sibs:
                mark = "→" if s.card_id == d.card_id else " "
                rng = f"R{{s.turn_start}}–R{{s.turn_end}}" if s.turn_start is not None else "R?"
                print(f"{{mark}} 📄 {{s.card_id}}  {{s.local_time}}  {{rng}}  {{s.headline}}")
        if args.before is not None:
            previous = retrieval.session_before(conn, args.card, limit=before_limit) or []
            scope = "全部" if before_limit is None else f"最近 {{before_limit}} 张"
            print(f"\\n── 同 session 此前卡片（{{scope}}，找到 {{len(previous)}} 张；远→近）──")
            for s in previous:
                rng = f"R{{s.turn_start}}–R{{s.turn_end}}" if s.turn_start is not None else "R?"
                print(f"📄 {{s.card_id}}  {{s.local_time}}  {{rng}}  {{s.headline}}")
            expand_count = args.expand or 0
            for s in previous[-expand_count:] if expand_count else []:
                print()
                _detail(s)
    elif args.time:
        since = args.time[0]
        until = args.time[1] if len(args.time) > 1 else None
        retrieval._print_cards(retrieval.recent(conn, since=since, until=until,
                               limit=max(args.limit, 40)))
    elif args.query:
        hits = retrieval.search(conn, args.query, limit=args.limit,
                                since=args.since, until=args.until)
        if not hits:
            print("（无命中）")
        retrieval._print_cards(hits)
        for ref in hits[: args.expand or 0]:
            d = retrieval.card_detail(conn, ref.card_id)
            if d:
                print()
                _detail(d)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
'''


_SUBMIT_TEMPLATE = '''#!/usr/bin/env python3
"""工作台提交口：夜间维护的结果从这里交，不要自己写文件。

用法：./submit result.json   或   cat result.json | ./submit
校验通过打印 PASS 并落 submission.json（重复提交覆盖）；不过打印 REJECTED
和逐条原因，改完重交。token 预算、字段、constant_id 都在这里审，交到 PASS 为止。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, {src!r})
import submitcheck  # noqa: E402


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        text = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    try:
        data = json.loads(text)
    except ValueError as e:
        print(f"REJECTED: 不是合法 JSON（{{e}}）")
        return 1
    try:
        basket = json.loads((HERE / "constants.json").read_text(encoding="utf-8"))
        known_ids = {{c.get("constant_id") for c in basket}}
    except (OSError, ValueError):
        known_ids = None
    errors = submitcheck.check(data, known_ids, max_tokens={digest_max})
    if errors:
        print("REJECTED:")
        for e in errors:
            print(f"- {{e}}")
        return 1
    (HERE / "submission.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(f"PASS: {{submitcheck.summary(data)}}。已提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def digest_max_tokens() -> int:
    """digest 的注入预算（token 估算值）。提交门、落盘裁剪、prompt 告知共用一处。
    2026-07-19 起从字符数改为 token：字数对模型没意义，校验端配 TOKEN_SLACK 留分词余地。"""
    mid = load_settings().get("midlayer", {})
    return int(mid.get("digest_max_tokens", mid.get("digest_max_chars", 1000)))


def curate_rooms() -> list[str]:
    """启用夜间 curator 的房间列表（**每房间开关**的落点）。

    settings.midlayer.enabled=false → 整体熄火返回 []。否则逐房看
    settings.midlayer.rooms[room]（bool），未列出的房间默认参与（True），
    这样新加房间自动跑、不用改 settings。
    """
    s = load_settings().get("midlayer", {})
    if not s.get("enabled", True):
        return []
    rooms_cfg = s.get("rooms", {})
    return [r for r in ROOMS if rooms_cfg.get(r, True)]


def fresh_card_count(conn: sqlite3.Connection, room: str, night: str) -> int:
    """保险拴：该 viewer 自上次成功 digest 以来的可见新卡数（无成功史则数全部）。

    为 0 的房间当晚整个跳过——不导出工作台、不调模型。fresh 分界与树报告同一条
    （treesnap._fresh_cutoff：成功夜的次日本地零点），保证「报告里没有新卡的线」
    和「跳过」永远一致。
    """
    visible, params = card_visible_clause(room, "c")
    sql = f"SELECT COUNT(*) FROM cards c WHERE {visible}"
    last_success = _last_digest_night(conn, room, night)
    if last_success:
        sql += " AND c.timestamp > ?"
        params = [*params, treesnap._fresh_cutoff(last_success)]
    return conn.execute(sql, params).fetchone()[0]


def _last_digest_night(conn: sqlite3.Connection, room: str, night: str) -> str | None:
    """该房间当前 night 之前最近一次真正成功落盘的 curator 夜。"""
    row = conn.execute(
        "SELECT MAX(night) AS n FROM digests WHERE room = ? AND night < ?",
        (room, night),
    ).fetchone()
    return row["n"] if row and row["n"] else None


def export_workbench(conn: sqlite3.Connection, room: str, night: str,
                     db_path: Path = DB) -> Path:
    """导出一个房间的工作台到 data/curator/<night>/<room>/，返回目录。"""
    dest = CURATOR_DIR / night / room
    dest.mkdir(parents=True, exist_ok=True)

    _export_view_db(conn, room, dest / "view.db", db_path)
    report = treesnap.render_report(
        conn, night=night, viewer=room,
        fresh_after_night=_last_digest_night(conn, room, night),
        fresh_from_success=True,
    )
    (dest / "tree-report.md").write_text(report, encoding="utf-8")
    # 来路图：digest 是树的每夜投影（2026-07-19 十炉＋K 炉），主线来龙去脉的骨架就是
    # 这张表——每个子簇给首末卡 headline 弧线，材料形状即目标形状。从本房 view.db
    # 现算，编号保证可被工作台 ./recall 下钻。单独落盘，注入 {tree-full-picture}。
    (dest / "tree-full-picture.md").write_text(
        _render_mainline_paths(dest / "view.db", night), encoding="utf-8")
    (dest / "constants.json").write_text(
        json.dumps(_room_constants(conn, room), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 不再回喂昨晚 digest：树＋constants 是唯一真身，digest 每夜白纸重写（防措辞在
    # 滚动改写里硬化变形）。digests 表照写，只存档供翻阅。
    (dest / "prev-digest.md").unlink(missing_ok=True)
    for name, template in (("recall", _RECALL_TEMPLATE), ("submit", _SUBMIT_TEMPLATE)):
        tool = dest / name
        tool.write_text(
            template.format(src=str(SRC_DIR.resolve()), digest_max=digest_max_tokens()),
            encoding="utf-8",
        )
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # 同夜重跑时不让上一轮的提交冒充这一轮的结果
    (dest / "submission.json").unlink(missing_ok=True)
    return dest


def _export_view_db(conn: sqlite3.Connection, room: str, dest: Path, db_path: Path) -> None:
    """viewer 过滤的库拷贝：不可见卡不进来，他房 shared 卡 private 置空。纯 SQL 导出。"""
    slug = ROOM_SLUGS.get(room, room)
    visible, vparams = card_visible_clause(room, "c")
    if dest.exists():
        dest.unlink()
    v = sqlite3.connect(dest)
    try:
        v.executescript(_VIEW_SCHEMA)
        v.execute("ATTACH DATABASE ? AS src", (str(db_path),))
        # 可见卡；本房卡留 private，他房（shared）卡 private 置空。
        v.execute(
            f"""
            INSERT INTO cards (card_id, session_id, turn_start, turn_end, headline, share,
                               private, timestamp, room, model)
            SELECT c.card_id, c.session_id, c.turn_start, c.turn_end, c.headline, c.share,
                   CASE WHEN c.room = ? THEN c.private ELSE '' END,
                   c.timestamp, c.room, c.model
            FROM src.cards c
            WHERE {visible}
            """,
            (slug, *vparams),
        )
        # card_tags / cluster_members 只带入已进库的卡；clusters/entities/tag_entity_map 照拷。
        v.execute("INSERT INTO card_tags SELECT ct.card_id, ct.tag FROM src.card_tags ct WHERE ct.card_id IN (SELECT card_id FROM cards)")
        v.execute("INSERT INTO clusters SELECT cluster_id, summary, parent_cluster_id, level FROM src.clusters")
        v.execute("INSERT INTO cluster_members SELECT cm.card_id, cm.cluster_id, cm.role FROM src.cluster_members cm WHERE cm.card_id IN (SELECT card_id FROM cards)")
        v.execute("INSERT INTO entities SELECT entity_id, canonical_name, name_embedding, created_at FROM src.entities")
        v.execute("INSERT INTO tag_entity_map SELECT tag, entity_id FROM src.tag_entity_map")
        # FTS 按 card_id 拷行（分词现成不重算）；他房 private 同样置空。
        v.execute(
            """
            INSERT INTO cards_fts (card_id, headline, share, private)
            SELECT f.card_id, f.headline, f.share,
                   CASE WHEN c.room = ? THEN f.private ELSE '' END
            FROM src.cards_fts f JOIN cards c ON c.card_id = f.card_id
            """,
            (slug,),
        )
        v.commit()               # 提交 src 读事务后才能 DETACH
        v.execute("DETACH DATABASE src")
    finally:
        v.close()


def _render_mainline_paths(view_db: Path, night: str) -> str:
    """「各主线的来路」：每条顶层主线按出生时间列子簇，每个子簇给"首卡 ⇢ 末卡"的
    headline 弧线（亮着的多带一条近况）——材料形状即目标形状，远处压缩近处详细
    （2026-07-19 K 炉定型）。末活距 night 六天内算亮。全部编号取自 view.db 自身，
    保证工作台 ./recall --cluster 能钻（快照层编号错位问题不波及本层）。"""
    import datetime
    import re as _re

    bright_since = (
        datetime.date.fromisoformat(night) - datetime.timedelta(days=6)
    ).isoformat()
    v = sqlite3.connect(view_db)
    try:
        def sub_cards(cid: str) -> list:
            return v.execute(
                """
                WITH RECURSIVE sub(id) AS (
                  SELECT ? UNION SELECT cluster_id FROM clusters, sub
                  WHERE parent_cluster_id = sub.id)
                SELECT c.headline, date(c.timestamp) d
                FROM cards c JOIN cluster_members m ON c.card_id = m.card_id
                WHERE m.cluster_id IN (SELECT id FROM sub) AND c.timestamp IS NOT NULL
                ORDER BY c.timestamp
                """,
                (cid,),
            ).fetchall()

        def kw(summary: str, n: int = 4) -> str:
            return "/".join(_re.sub(r"\(\d+\)", "", w) for w in summary.split()[:n])

        lines = ["各主线的来路（子簇按时间排；⇢连着的是该段首末两张卡；编号可 ./recall --cluster 下钻）："]
        for rid, rsum in v.execute(
            "SELECT cluster_id, summary FROM clusters WHERE parent_cluster_id IS NULL"
        ):
            cards = sub_cards(rid)
            if not cards:
                continue
            lines.append(f"  {rid} {kw(rsum)}  {len(cards)}卡 {cards[0][1]}起")
            kids = []
            for kid, ksum in v.execute(
                "SELECT cluster_id, summary FROM clusters WHERE parent_cluster_id = ?",
                (rid,),
            ):
                kc = sub_cards(kid)
                if len(kc) >= 4:
                    kids.append((kid, ksum, kc))
            seen_heads: set[str] = set()
            for kid, ksum, kc in sorted(kids, key=lambda k: k[2][0][1]):
                lo, hi = kc[0][1], kc[-1][1]
                bright = hi >= bright_since
                state = f"活到{hi[5:]}" if bright else f"{hi[5:]}熄"
                # 兄弟子簇撞首卡（聚类重叠）：换下一张没用过的当起点
                head = next((h for h, _ in kc if h not in seen_heads), kc[0][0])
                seen_heads.add(head)
                lines.append(f"    {kid}（{lo[5:]}生·{state}，{len(kc)}卡）{head} ⇢ {kc[-1][0]}")
                # 近况和末卡一字不差（重复卡）就不重复打
                if bright and len(kc) >= 3 and kc[-2][0] != kc[-1][0]:
                    lines.append(f"        近况：{kc[-2][0]}")
        return "\n".join(lines) + "\n"
    finally:
        v.close()


def _room_constants(conn: sqlite3.Connection, room: str) -> list[dict]:
    """现有篮子：shared 全部 + 本房 private 的 active 条目（本次 constants 表为空，返回空）。"""
    rows = conn.execute(
        """
        SELECT constant_id, room, shared, content, source_card_id
        FROM constants
        WHERE status = 'active' AND (shared = 1 OR room = ?)
        ORDER BY first_seen
        """,
        (room,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_all_workbenches(conn: sqlite3.Connection, night: str | None = None,
                           db_path: Path = DB) -> list[Path]:
    """对所有启用的房间导出工作台，并清理过期夜。返回导出的目录列表。"""
    night = night or treesnap.tonight_night()
    rooms = curate_rooms()
    dests = [export_workbench(conn, room, night, db_path) for room in rooms]
    cleanup_old_workbenches()
    return dests


def cleanup_old_workbenches(keep_nights: int | None = None) -> list[str]:
    """删 data/curator/ 下超出 keep_nights 的夜目录（与快照保留同一参数）。"""
    if keep_nights is None:
        keep_nights = load_settings().get("midlayer", {}).get("snapshot_keep_nights", 14)
    if not CURATOR_DIR.exists() or not keep_nights or keep_nights <= 0:
        return []
    nights = sorted((p.name for p in CURATOR_DIR.iterdir() if p.is_dir()), reverse=True)
    pruned = nights[keep_nights:]
    for n in pruned:
        shutil.rmtree(CURATOR_DIR / n, ignore_errors=True)
    return pruned


# ---- 阶段 3：curator agent 编排 ----

@functools.lru_cache(maxsize=1)
def _prompt_template() -> str:
    """读取 curator prompt 的当前运行模板（优先取 markdown 代码框）。"""
    if not PROMPT_FILE.exists():
        return ""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    block = re.search(r"##\s*当前运行prompt版本.*?```[a-zA-Z]*\n(.*?)\n```", text, re.S)
    return block.group(1).strip() if block else text.strip()


def _workbench_text(dest: Path, name: str, fallback: str = "（无）") -> str:
    path = dest / name
    if not path.exists():
        return fallback
    text = path.read_text(encoding="utf-8").strip()
    return text or fallback


def _split_tree_report(text: str) -> tuple[str, str]:
    """把 tree-report 拆成新动静（diff）和顶层热度（heat）。"""
    marker = "今晚有新卡的线"
    pos = text.find(marker)
    if pos == -1:
        return text.strip() or "（无）", "（无）"
    return text[pos:].strip() or "（无）", text[:pos].strip() or "（无）"


def _fill_prompt(room: str, night: str, dest: Path) -> str:
    persona = _agent_persona_file(room).read_text(encoding="utf-8").strip()
    constants = _workbench_text(dest, "constants.json")
    digest = _workbench_text(dest, "prev-digest.md")
    tree_diff, tree_heat = _split_tree_report(_workbench_text(dest, "tree-report.md"))
    # 来路图取代 heat 段进 prompt（heat 的顶层社区表冗余且编号来自快照层，来路图
    # 编号全部来自 view.db）；{tree-heat} 留着只为兼容旧模板。
    full_picture = _workbench_text(dest, "tree-full-picture.md")
    # {username}/{digest-max} 只在模板层替换，先于注入的工作台内容。
    template = fill_username(_prompt_template())
    template = template.replace("{digest-max}", str(digest_max_tokens())) \
                       .replace("{digest_max}", str(digest_max_tokens()))
    body = (template
            .replace("{agent-persona}", persona)
            .replace("{agent_persona}", persona)
            .replace("{pre-constant}", constants)
            .replace("{pre_constant}", constants)
            .replace("{pre-digest}", digest)
            .replace("{pre_digest}", digest)
            .replace("{tree-diff}", tree_diff)
            .replace("{tree_diff}", tree_diff)
            .replace("{tree-heat}", tree_heat)
            .replace("{tree_heat}", tree_heat)
            .replace("{tree-full-picture}", full_picture)
            .replace("{tree_full_picture}", full_picture))
    return f"# 今晚 {night}，房间：{room}\n\n{body}"


def run_curation(
    conn: sqlite3.Connection,
    model_factory: Callable[[str], ModelRunner],
    night: str | None = None,
    db_path: Path = DB,
) -> list[dict]:
    """每房间一次 agent 调用：看树 → 下钻 → 写近况小结 + constants 操作，一次返回。

    按配置顺序逐房间跑；每个房间**先导出工作台再调用**——这样后跑的房间能看到先跑房间
    刚写入的 shared constant（共用篮子天然去重）。模型自己不写任何文件，
    所有落盘由本函数解析 stdout 后执行。model_factory(cwd) 按工作台目录构造模型。
    """
    night = night or treesnap.tonight_night()
    rooms = curate_rooms()
    persona_files = [_agent_persona_file(room) for room in rooms]
    run_id = create_pipeline_run(conn, "curate", PROMPT_FILE, persona_files)
    results = []
    for room in rooms:
        n_fresh = fresh_card_count(conn, room, night)
        if not n_fresh:
            results.append({"room": room, "skipped": "no-fresh-cards"})
            continue
        dest = export_workbench(conn, room, night, db_path)  # 含先前房间的 shared constant
        model = model_factory(str(dest))
        prompt = _fill_prompt(room, night, dest)
        try:
            raw = model.run(prompt)
        except Exception as error:
            _record_model_attempts(conn, run_id, room, prompt, model)
            # 非标准/旧 model_factory 仍可能在 ./submit 已落盘后因空 stdout 抛错；
            # 提交在且过复验就不算失败——submission 才是门。标准 CLIModel 已通过
            # success_artifact 在首次 rc=0 后直接返回，不再走四次退避重试。
            parsed = _read_submission(conn, room, dest)
            if parsed is None:
                record_model_call(
                    conn, run_id, f"curate:{room}:error", prompt,
                    f"(error: {error})", {"error": str(error)}, session_id=room,
                )
                conn.commit()
                raise
            raw = f"(stdout empty, recovered from submission.json: {error})"
            submitted = True
        else:
            _record_model_attempts(conn, run_id, room, prompt, model)
            # 结果优先走 ./submit 落的 submission.json——wrapper 只是给模型的即时反馈，
            # 这里的复验才是门（沙盒里绕过 submit 直接写的文件同样要过 check）。
            parsed = _read_submission(conn, room, dest)
            submitted = parsed is not None
            if not submitted:
                parsed = _parse_output(raw)  # 没提交/复验不过：退回 stdout 解析的旧路径
                known_ids = {c["constant_id"] for c in _room_constants(conn, room)}
                errors = submitcheck.check(parsed, known_ids, max_tokens=digest_max_tokens())
                if errors:
                    record_model_call(
                        conn, run_id, f"curate:{room}:error", prompt, raw,
                        {"errors": errors, "parsed": parsed}, session_id=room,
                    )
                    conn.commit()
                    raise RuntimeError("curator produced no valid submission: " + "; ".join(errors))
        record_model_call(conn, run_id, f"curate:{room}", prompt, raw, parsed, session_id=room)
        digest = _limit_digest(parsed.get("digest") or "")
        ops = parsed.get("constants") or []
        if digest:
            _write_digest(conn, room, night, digest, model.name)
        applied = _apply_constants_ops(conn, room, ops)
        # constants.md 渲染：优先用模型组织的 constants_md（省 token），没有则机械 fallback；
        # 篮子当晚无变动且模型没给组织版 → 不重写（多数晚上零成本）。
        constants_md = parsed.get("constants_md")
        changed = any(applied[k] for k in ("add", "update", "retire"))
        if isinstance(constants_md, str) and constants_md.strip():
            _write_constants_md(room, constants_md)
        elif changed:
            render_constants_md(conn, room)
        conn.commit()
        results.append({"room": room, "digest_chars": len(digest), "submitted": submitted,
                        "constants": applied, "constants_md": bool(constants_md)})
    cleanup_old_workbenches()
    return results


def _record_model_attempts(conn: sqlite3.Connection, run_id: str, room: str,
                           prompt: str, model: ModelRunner) -> None:
    """把 CLIModel 内部每次物理 subprocess 尝试单独留档，避免四次重试看成一次。"""
    attempts = model_attempts(model)
    for attempt in attempts:
        stdout = str(attempt.get("stdout") or "")
        stderr = str(attempt.get("stderr") or "")
        meta = {k: v for k, v in attempt.items() if k not in ("stdout", "stderr")}
        record_model_call(
            conn, run_id, f"curate:{room}:attempt", prompt,
            stdout or (f"(error: {stderr})" if stderr else ""), meta,
            session_id=room,
        )
    if attempts:
        conn.commit()


def _agent_persona_file(room: str) -> Path:
    """该房间出卡用的 persona；curator 用它判断什么值得留下。"""
    return MEMORY / "prompts" / f"agent-persona-{room}.md"


def _read_submission(conn: sqlite3.Connection, room: str, dest: Path) -> dict | None:
    """读工作台的 submission.json 并复验；没有或不过关返回 None（走 stdout 旧路径）。"""
    path = dest / "submission.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known_ids = {c["constant_id"] for c in _room_constants(conn, room)}
    ok = isinstance(data, dict) and not submitcheck.check(data, known_ids, max_tokens=digest_max_tokens())
    return data if ok else None


def _parse_output(text: str) -> dict:
    """从模型输出里抽出 JSON（优先 ```json 代码块，退化到首尾花括号）。"""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None:
        start, end = text.find("{"), text.rfind("}")
        blob = text[start : end + 1] if start != -1 and end > start else ""
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _limit_digest(text: str, max_tokens: int | None = None) -> str:
    """保证 digest 不超过注入预算（token 估算，含校验同款宽限）；优先按段落裁掉尾部。"""
    if max_tokens is None:
        max_tokens = digest_max_tokens()
    allowed = int(max_tokens * submitcheck.TOKEN_SLACK)
    digest = text.strip()
    if submitcheck.estimate_tokens(digest) <= allowed:
        return digest
    kept: list[str] = []
    for para in re.split(r"\n{2,}", digest):
        para = para.strip()
        if not para:
            continue
        candidate = "\n\n".join(kept + [para])
        if submitcheck.estimate_tokens(candidate) > allowed:
            break
        kept.append(para)
    if kept:
        return "\n\n".join(kept)
    return digest[:allowed].rstrip()


def _write_digest(conn: sqlite3.Connection, room: str, night: str, body: str, model: str) -> None:
    """近况小结：覆盖式写 room_dir/digest.md + digests 表存历史。"""
    conn.execute(
        "INSERT OR REPLACE INTO digests (night, room, body, model) VALUES (?, ?, ?, ?)",
        (night, room, body, model),
    )
    path = ROOM_DIRS[room] / "digest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 近况（更新至 {night}）\n\n> 夜间 curator 根据当前记忆树重写\n\n{body}\n",
                    encoding="utf-8")


def _apply_constants_ops(conn: sqlite3.Connection, room: str, ops: list) -> dict:
    """把 add / update / retire 增量指令落到 constants 表。返回各类计数。"""
    now = dt.datetime.now(dt.UTC).isoformat()
    counts = {"add": 0, "update": 0, "retire": 0, "skipped": 0}
    for op in ops:
        if not isinstance(op, dict):
            counts["skipped"] += 1
            continue
        kind = op.get("op")
        if kind == "add":
            content = (op.get("content") or "").strip()
            if not content:
                counts["skipped"] += 1
                continue
            conn.execute(
                """
                INSERT INTO constants
                (constant_id, room, shared, content, source_card_id, status, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                ("k_" + uuid.uuid4().hex[:8], room, 1 if op.get("shared") else 0,
                 content, op.get("source_card_id"), now, now),
            )
            counts["add"] += 1
        elif kind == "update" and op.get("constant_id"):
            sets, params = [], []
            if "content" in op:
                sets.append("content = ?")
                params.append((op.get("content") or "").strip())
            if "shared" in op:
                sets.append("shared = ?")
                params.append(1 if op.get("shared") else 0)
            if not sets:
                counts["skipped"] += 1
                continue
            sets.append("updated_at = ?")
            params.extend([now, op["constant_id"], room])
            cur = conn.execute(
                f"UPDATE constants SET {', '.join(sets)} WHERE constant_id = ? AND room = ?",
                params,
            )
            counts["update" if cur.rowcount else "skipped"] += 1
        elif kind == "retire" and op.get("constant_id"):
            cur = conn.execute(
                "UPDATE constants SET status='retired', updated_at=? WHERE constant_id=? AND room=?",
                (now, op["constant_id"], room),
            )
            counts["retire" if cur.rowcount else "skipped"] += 1
        else:
            counts["skipped"] += 1
    return counts


def render_constants_md(conn: sqlite3.Connection, room: str) -> None:
    """重渲染 room_dir/constants.md：全院 shared + 本房 private 的 active 条目。"""
    shared = conn.execute(
        "SELECT content FROM constants WHERE status='active' AND shared=1 ORDER BY first_seen"
    ).fetchall()
    private = conn.execute(
        "SELECT content FROM constants WHERE status='active' AND shared=0 AND room=? ORDER BY first_seen",
        (room,),
    ).fetchall()
    lines = [f"# constants — {room}", "",
             "> 永久有效的事实篮子，夜间 curator 维护。shared 段全院可见。", ""]
    lines.append("## 全院共享")
    lines += [f"- {r['content']}" for r in shared] or ["- （空）"]
    lines.append("")
    lines.append(f"## 本房（{room}）")
    lines += [f"- {r['content']}" for r in private] or ["- （空）"]
    path = ROOM_DIRS[room] / "constants.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_constants_md(room: str, text: str) -> None:
    """写模型组织好的 constants.md（常驻注入版，token 已由模型压到最省）。

    这是渲染层，供住户 `@` 进 CLAUDE.md；机械 render_constants_md 是其 fallback，
    保证文件永远和 constants 表一致可重建。
    """
    path = ROOM_DIRS[room] / "constants.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
