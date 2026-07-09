"""增量 ingest：只重读 mtime 变过的文件，其余走库。

背景：watcher 每次文件变化都全量重读所有房间 JSONL（197MB→12k 消息，其中绝大部分是
被丢弃的工具输出），只为兜住 fork/撤回带来的重放与重编号。但冷 session 的文件一旦定稿
就永不再变，重读它们纯属浪费，且随历史线性变慢。

正确性关键：round/message_seq 由 assign_rounds 按 session_id 从库里加载的行**整体**重编号；
一个 session 的行可能因 fork/续接被 Claude Code 劈到多个文件（重放行保留原 sessionId）。
所以不能只读变化文件——那样劈开的 session 会被局部重编号而写坏库。做法是取
(文件 ↔ session_id) 二部图中、包含任一变化文件的**连通分量**里的所有文件一起加载：

    变化文件 → 它们牵出的 session → 这些 session 的所有文件 → 又牵出的 session → …（到不动点）

于是每个被触碰的 session 都从它的全部文件一起重编号，与全量重读结果一致。

状态存 `<db 同目录>/ingest-state.json`（{文件路径: mtime}）。删掉它即强制下次全量重读。
fork 检测（refresh_session_forks）只读库不读文件，故与本模块无关，照常工作。
"""
from __future__ import annotations

import json
from pathlib import Path

from db import files_for_sessions, sessions_in_files
from loaders import session_ids_in_paths

STATE_NAME = "ingest-state.json"
_MAX_ITERS = 50  # 连通分量扩张的安全上限；正常 1~2 轮即收敛


def state_path_for(db_path: Path) -> Path:
    return db_path.parent / STATE_NAME


def load_state(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, mtimes: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mtimes, ensure_ascii=False), encoding="utf-8")


def current_mtimes(paths: list[Path]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return out


def select_incremental(conn, all_paths: list[Path], state: dict[str, float]) -> tuple[list[Path], dict[str, float]]:
    """返回 (要加载的文件, 全量当前 mtime 快照)。

    第二个返回值记录**磁盘上所有文件**的当前 mtime（删除的文件自然掉出），ingest 成功后
    写回状态。无变化时返回 ([], 快照)。首跑（state 空）时所有文件都算变化 → 退化为全量。
    """
    current = current_mtimes(all_paths)
    by_str = {str(p): p for p in all_paths}
    changed = {s for s, m in current.items() if state.get(s) != m}
    if not changed:
        return [], current

    comp = set(changed)
    # 种子 session：变化文件在磁盘上直接解析出的（覆盖库里还没有的全新 fork 子文件），
    # 加上库里已归属这些文件的 session。
    frontier = session_ids_in_paths([by_str[s] for s in changed if s in by_str])
    frontier |= sessions_in_files(conn, changed)
    seen: set[str] = set()
    for _ in range(_MAX_ITERS):
        if not frontier:
            break
        seen |= frontier
        new_files = {f for f in files_for_sessions(conn, frontier) if f in by_str} - comp
        comp |= new_files
        if not new_files:
            break
        frontier = sessions_in_files(conn, new_files) - seen

    return [by_str[s] for s in comp if s in by_str], current
