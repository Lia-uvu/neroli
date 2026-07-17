"""事件卡生成：滚动窗口 + 冻结-重喂机制。

从 pipeline-lab/cluster_lab/gen_cards.py 移植。
核心算法：
  1. 把 session 的 turns 按 init_rounds 轮一批喂模型出卡。
  2. 冻结除最后一张卡以外的所有卡；从倒数第二张卡的 turn_end 重喂，
     让最后一张卡和新 turns 一起重判，避免窗口边界切断话题。
  3. 重复直到所有 turns 处理完毕。
"""
from __future__ import annotations

import functools
import re
import sqlite3
from collections.abc import Callable

from config import DEFAULT_ROOM, MEMORY, agent_name, fill_username, load_settings, user_name
from db import delete_card, get_session_cards, get_session_fork, insert_card, load_turns_from_round, record_model_call
from memory_types import Message
from model import ModelRunner, model_attempts

PROMPTS_DIR = MEMORY / "prompts"
AGENT_PERSONA_PATTERN = "agent-persona-{room}.md"
PROMPT_FILE = PROMPTS_DIR / "gen-cards-prompt.md"

CARD_RE = re.compile(r"turns?\s*[:：]\s*R?([0-9]+)\s*[-–]?\s*R?([0-9]*)", re.I)
ModelCallObserver = Callable[
    [str, str, list[dict], Exception | None, list[dict[str, object]]], None
]


def _settings_card_gen() -> dict:
    return load_settings().get("card_gen", {})


@functools.lru_cache(maxsize=1)
def _prompt_template() -> str:
    if not PROMPT_FILE.exists():
        return ""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    block = re.search(r"##\s*当前运行prompt版本.*?```[a-zA-Z]*\n(.*?)\n```", text, re.S)
    return block.group(1).strip() if block else ""


@functools.lru_cache(maxsize=None)
def _agent_persona(room: str) -> str:
    path = PROMPTS_DIR / AGENT_PERSONA_PATTERN.format(room=room)
    if not path.exists() and room != DEFAULT_ROOM:
        path = PROMPTS_DIR / AGENT_PERSONA_PATTERN.format(room=DEFAULT_ROOM)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _render_window(messages: list[Message], lo: int, hi: int, turn_cap: int = 1600) -> str:
    lines: list[str] = []
    cur = None
    u_name, a_name = user_name(), agent_name()
    for m in messages:
        if m.round < lo or m.round > hi:
            continue
        if m.round != cur:
            cur = m.round
            lines.append(f"\nR{cur}")
        who = u_name if m.role == "user" else a_name
        txt = m.text.strip()
        if len(txt) > turn_cap:
            txt = txt[:turn_cap] + " …(截断)"
        lines.append(f"{who}: {txt}")
    return "\n".join(lines).strip()


def _ordered_rounds(messages: list[Message]) -> list[int]:
    seen: list[int] = []
    for m in messages:
        if m.round not in seen:
            seen.append(m.round)
    return seen


def parse_cards(text: str) -> list[dict]:
    idxs = [m.start() for m in re.finditer(r"(?mi)^\s*turns?\s*[:：]\s*R?[0-9]+", text)]
    if not idxs:
        return []
    idxs.append(len(text))
    cards = []
    for a, b in zip(idxs, idxs[1:]):
        block = text[a:b].strip()
        m = CARD_RE.search(block)
        lo = int(m.group(1)) if m else None
        hi = int(m.group(2)) if (m and m.group(2)) else lo

        def field(name, blk=block):
            mm = re.search(
                rf"(?mi)^[ \t]*{name}[ \t]*[:：][ \t]*(.*?)(?=\n[ \t]*(?:theme|share|private|tags|turns?)[ \t]*[:：]|\Z)",
                blk, re.S,
            )
            return mm.group(1).strip() if mm else ""

        cards.append({
            "turns": [lo, hi],
            "theme": field("theme"),
            "share": field("share"),
            "private": field("private"),
            "tags": [t for t in re.split(r"[/、,，\s]+", field("tags")) if t],
            "raw": block,
        })
    return cards


def _card_memory_text(card: dict) -> str:
    parts = [
        (card.get("share") or "").strip(),
        (card.get("private") or "").strip(),
    ]
    text = "\n".join(part for part in parts if part)
    if text:
        return text
    return (card.get("raw") or "").strip()


def _cards_to_summary(cards: list[dict]) -> str:
    if not cards:
        return "（无）"
    chunks = [_card_memory_text(c) for c in cards]
    return "\n\n".join(chunk for chunk in chunks if chunk) or "（无）"


def _fill_prompt(existing: str, conversation: str, room: str) -> str:
    # {username} 只在模板层替换，先于注入——对话正文/persona 里的字面文本不受波及。
    return (fill_username(_prompt_template())
            .replace("{agent-persona}", _agent_persona(room))
            .replace("{agent_persona}", _agent_persona(room))
            .replace("{existing_summary}", existing or "（无）")
            .replace("{conversation}", conversation))


def generate(
    messages: list[Message],
    model: ModelRunner,
    init_rounds: int | None = None,
    grow: int | None = None,
    prior_summary: str = "",
    room: str = DEFAULT_ROOM,
    call_observer: ModelCallObserver | None = None,
) -> list[dict]:
    s = _settings_card_gen()
    if init_rounds is None:
        init_rounds = s.get("init_rounds", 14)
    if grow is None:
        grow = s.get("grow_rounds", 14)
    turn_cap = s.get("turn_cap", 1600)

    rounds = _ordered_rounds(messages)
    if not rounds:
        return []
    n = len(rounds)

    def call(existing: str, conversation: str) -> list[dict]:
        prompt = _fill_prompt(existing, conversation, room)
        raw = ""
        cards: list[dict] = []
        error: Exception | None = None
        try:
            raw = model.run(prompt)
            cards = parse_cards(raw)
            if not cards:
                raise RuntimeError("model output contained no parseable cards")
        except Exception as exc:
            error = exc
            raise
        finally:
            if call_observer is not None:
                call_observer(prompt, raw, cards, error, model_attempts(model))
        return cards

    if n <= init_rounds:
        conv = _render_window(messages, rounds[0], rounds[-1], turn_cap)
        return call(prior_summary or "（无）", conv)

    frozen: list[dict] = []
    seen_hi_idx = init_rounds - 1
    conv = _render_window(messages, rounds[0], rounds[seen_hi_idx], turn_cap)
    cards = call(prior_summary or "（无）", conv)

    while seen_hi_idx < n - 1:
        if len(cards) >= 2:
            frozen = frozen + cards[:-1]
            refeed_lo = cards[-2]["turns"][1] or cards[-1]["turns"][0] or rounds[seen_hi_idx]
        else:
            # Bootstrap: until the model has split the opening into at least
            # two cards, keep the whole current raw prefix in view.
            refeed_lo = rounds[0]
        new_hi_idx = min(seen_hi_idx + grow, n - 1)
        conv = _render_window(messages, refeed_lo, rounds[new_hi_idx], turn_cap)
        all_frozen = ([{"raw": prior_summary}] if prior_summary else []) + frozen
        existing = _cards_to_summary(all_frozen) if all_frozen else "（无）"
        cards = call(existing, conv)
        seen_hi_idx = new_hi_idx

    return frozen + cards


def process_session_cards(
    conn: sqlite3.Connection,
    run_id: str,
    session_id: str,
    model: ModelRunner,
    room: str = DEFAULT_ROOM,
) -> list[dict]:
    return _rewrite_session_tail(conn, run_id, session_id, model, room, allow_noop=False)


def update_session_cards(
    conn: sqlite3.Connection,
    run_id: str,
    session_id: str,
    model: ModelRunner,
    room: str = DEFAULT_ROOM,
) -> list[dict]:
    """增量更新：重写最后一张卡，保留已冻结的卡。"""
    return _rewrite_session_tail(conn, run_id, session_id, model, room, allow_noop=True)


def _rewrite_session_tail(
    conn: sqlite3.Connection,
    run_id: str,
    session_id: str,
    model: ModelRunner,
    room: str,
    *,
    allow_noop: bool,
) -> list[dict]:
    own_cards = get_session_cards(conn, session_id)
    effective_cards = _effective_existing_cards(conn, session_id, own_cards)
    all_messages = load_turns_from_round(conn, session_id, 1)
    if not all_messages:
        return []

    max_round = max(m.round for m in all_messages)
    last_card = effective_cards[-1] if effective_cards else None
    covered_round = _covered_round_for(session_id, last_card)
    if allow_noop and covered_round and covered_round >= max_round:
        return []

    if len(effective_cards) >= 2:
        frozen_rows = effective_cards[:-1]
        refeed_from = frozen_rows[-1]["turn_end"] or 1
    else:
        frozen_rows = []
        fork = get_session_fork(conn, session_id)
        refeed_from = fork["delta_start_round"] if fork else 1

    frozen_dicts = _rows_to_card_dicts(conn, frozen_rows)
    prior_summary = _cards_to_summary(frozen_dicts) if frozen_dicts else ""

    messages = load_turns_from_round(conn, session_id, refeed_from)
    if not messages:
        return []

    window_no = 0

    def audit_call(prompt: str, raw: str, parsed: list[dict], error: Exception | None,
                   attempts: list[dict[str, object]]) -> None:
        nonlocal window_no
        window_no += 1
        rows = attempts or [{
            "attempt": 1,
            "status": "error" if error else "success",
            "returncode": None,
            "stdout": raw,
            "stderr": str(error) if error else "",
        }]
        for attempt in rows:
            stdout = str(attempt.get("stdout") or "")
            stderr = str(attempt.get("stderr") or "")
            raw_attempt = stdout or (f"(error: {stderr or error})" if error or stderr else "")
            meta = {
                "window": window_no,
                "attempt": attempt.get("attempt"),
                "status": attempt.get("status"),
                "returncode": attempt.get("returncode"),
                "parse_error": str(error) if error else None,
                "cards": parsed if not error else [],
            }
            record_model_call(
                conn, run_id,
                "gen_cards_attempt_error" if error else "gen_cards_attempt",
                prompt, raw_attempt, meta, session_id=session_id,
            )
        # 审计先于业务替换独立提交：后窗失败时，前面已经付费的输出仍可追查，
        # 而旧卡尚未被触碰，不会被这个 commit 提前落成半套业务状态。
        conn.commit()

    new_cards = generate(
        messages, model, prior_summary=prior_summary, room=room,
        call_observer=audit_call,
    )

    if last_card and last_card["session_id"] == session_id:
        delete_card(conn, last_card["card_id"])

    card_offset = sum(1 for row in frozen_rows if row["session_id"] == session_id)
    for i, c in enumerate(new_cards):
        c["session_id"] = session_id
        c["timestamp"] = _timestamp_for_card(messages, c)
        c["card_id"] = f"{session_id[:8]}#{card_offset + i + 1}"
        c["room"] = room
        c["model"] = model.name
        insert_card(conn, c, label="pipeline", source_file="")

    rewrote_own_card = bool(last_card and last_card["session_id"] == session_id)
    step = "gen_cards_update" if rewrote_own_card else "gen_cards"
    summary = f"rewrite: -1 +{len(new_cards)} cards" if rewrote_own_card else f"{len(new_cards)} cards"
    record_model_call(
        conn, run_id, step, f"session={session_id}",
        summary, new_cards, session_id=session_id,
    )
    conn.commit()
    return new_cards


def _effective_existing_cards(
    conn: sqlite3.Connection,
    session_id: str,
    own_cards: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    fork = get_session_fork(conn, session_id)
    if not fork:
        return own_cards

    parent_cards = conn.execute(
        """
        SELECT * FROM cards
        WHERE session_id = ? AND turn_start <= ?
        ORDER BY turn_start
        """,
        (fork["parent_session_id"], fork["parent_fork_round"]),
    ).fetchall()

    if own_cards:
        first_own_start = own_cards[0]["turn_start"] or fork["delta_start_round"]
        parent_cards = [
            row for row in parent_cards
            if (row["turn_end"] or 0) <= first_own_start
        ]

    return [*parent_cards, *own_cards]


def _covered_round_for(session_id: str, card: sqlite3.Row | None) -> int | None:
    if card is None:
        return None
    if card["session_id"] != session_id:
        # A parent card is only virtual context for a fork child. It should make
        # us refeed like normal update, not prove the child branch is complete.
        return None
    return card["turn_end"]


def _timestamp_for_card(messages: list[Message], card: dict) -> str | None:
    turns = card.get("turns") or []
    lo = turns[0] if turns else None
    hi = turns[1] if len(turns) > 1 else lo
    if lo is not None:
        for message in reversed(messages):
            if message.timestamp and message.round >= lo and (hi is None or message.round <= hi):
                return message.timestamp
    return next((m.timestamp for m in reversed(messages) if m.timestamp), None)


def _rows_to_card_dicts(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    result = []
    for r in rows:
        raw_row = conn.execute(
            "SELECT raw FROM card_raw WHERE card_id = ?", (r["card_id"],)
        ).fetchone()
        tags_rows = conn.execute(
            "SELECT tag FROM card_tags WHERE card_id = ?", (r["card_id"],)
        ).fetchall()
        result.append({
            "card_id": r["card_id"],
            "session_id": r["session_id"],
            "turns": [r["turn_start"], r["turn_end"]],
            "theme": r["theme"] or "",
            "share": r["share"] or "",
            "private": r["private"] or "",
            "tags": [t["tag"] for t in tags_rows],
            "raw": raw_row["raw"] if raw_row else "",
        })
    return result
