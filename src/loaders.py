from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from config import agent_name, user_name, user_source_label
from memory_types import Message

# ── v4 两阶段加载 ───────────────────────────────────────────────────────────
# 阶段一：candidate 加载器解析/过滤/抽字段，但 **不** 定 round/message_seq，只带一个可比较的
#         sort_key（JSONL=文件最早ts+line_no；导出=消息created_at+数组下标）。
# 阶段二：assign_rounds 对合并后的全量 candidate 先按 (session_id, source_uuid) 去重，再分会话
#         排序编号。轮次必须跨一个 source 家族的所有文件一次性算，不能在 per-file 加载器里算
#         （9 个 Claude Code 会话跨多文件、774 个 (session_id,source_uuid) 跨文件重复；两份导出
#         也整体重叠）。
#
# 三个 source 家族：
#   * Claude Code JSONL  — source_uuid=顶层 uuid，session_id=顶层 sessionId，按文件追加序排
#   * Claude.ai 导出      — source_uuid=chat_messages[].uuid（原生），session_id=会话 uuid，按消息序排
#   * normalized / test  — 无原生 id，用确定性哈希；自带显式 round，绕过 assign_rounds


def load_messages_for_ingest(paths: list[Path]) -> list[Message]:
    """ingest / dump-json 的统一入口：按家族路由到 candidate 加载器，合并后一次性编号。

    一次调用可混合家族（v4 重建会把 JSONL 房间和导出归档一起灌）——去重和分会话编号
    对并集天然正确。normalized/test 自带 round，直接产出 Message，不进 assign_rounds。
    """
    candidates: list[dict[str, Any]] = []
    explicit: list[Message] = []
    for path in paths:
        family = classify_source(path)
        if family == "jsonl":
            candidates.extend(load_claude_jsonl_candidates(path))
        elif family == "export":
            candidates.extend(load_claude_export_candidates(path))
        elif family == "normalized":
            explicit.extend(load_normalized_json(path))
        elif family == "test":
            explicit.extend(load_test_transcript(path))
    messages = assign_rounds(candidates) if candidates else []
    return messages + explicit


def session_ids_in_paths(paths: list[Path]) -> set[str]:
    """轻量提取一批文件里出现的 session_id（不定 round，供增量 ingest 算连通分量用）。

    一个文件可能带多个 session_id：fork/续接会把旧对话的行重放进新文件，重放行保留原
    sessionId。增量 ingest 靠这个把「变化文件牵出的 session」和「这些 session 的其余文件」
    连起来，保证跨文件的 session 每次都从它的全部文件一起重编号。
    """
    sids: set[str] = set()
    for path in paths:
        family = classify_source(path)
        if family == "jsonl":
            sids.update(c["session_id"] for c in load_claude_jsonl_candidates(path))
        elif family == "export":
            sids.update(c["session_id"] for c in load_claude_export_candidates(path))
        elif family == "normalized":
            sids.update(m.session_id for m in load_normalized_json(path))
        elif family == "test":
            sids.update(m.session_id for m in load_test_transcript(path))
    return sids


def load_messages(path: Path, max_messages: int) -> list[Message]:
    """单文件兼容入口（保留给临时/外部调用）。ingest 走 load_messages_for_ingest。"""
    messages = load_messages_for_ingest([path])
    if max_messages <= 0:
        return messages
    return messages[-max_messages:]


def classify_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return "test"
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return "normalized"
        return "export" if is_claude_export(data) else "normalized"
    return "jsonl"


def is_claude_export(data: Any) -> bool:
    return isinstance(data, list) and bool(data) and isinstance(data[0], dict) and "chat_messages" in data[0]


# ── Claude Code JSONL candidate 加载器 ──────────────────────────────────────

def load_claude_jsonl_candidates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 结构性非对话内容按顶层字段拦掉：
            #   toolUseResult=工具结果，isMeta=系统注入，
            #   isCompactSummary=压缩摘要，isVisibleInTranscriptOnly=仅 UI 提示，
            #   isApiErrorMessage / error / apiErrorStatus=API 报错。
            if "toolUseResult" in obj or obj.get("isMeta"):
                continue
            if obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly") or obj.get("isApiErrorMessage"):
                continue
            if obj.get("error") or obj.get("apiErrorStatus"):
                continue
            message = obj.get("message") or {}
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            text = text_from_content(content)
            if role == "user":
                text = clean_user_text(text)
            if not text:
                continue
            img = count_images(content)
            source = model_to_source(obj.get("model") or message.get("model"))
            rows.append(
                {
                    "role": role,
                    "text": text,
                    "timestamp": obj.get("timestamp") or "",
                    "session_id": obj.get("sessionId") or path.stem,
                    "source_file": str(path),
                    "source": source if role == "assistant" else user_source_label(),
                    "source_uuid": obj.get("uuid") or f"jsonl:{path.stem}:{idx}",
                    "parent_uuid": obj.get("parentUuid") or "",
                    "has_image": 1 if img else 0,
                    "image_count": img,
                    "line_no": idx,
                    "model": obj.get("model") or message.get("model") or "",
                }
            )
    # 多文件排序键：同会话跨文件按文件最早时间戳排，文件内按 line_no（追加序）。
    # 不用 timestamp 主排序——60% 文件时间戳非单调，但追加序 0 因果倒置。
    earliest = min((r["timestamp"] for r in rows if r["timestamp"]), default="")
    for r in rows:
        r["sort_key"] = (earliest, r["line_no"])
    return rows


# ── Claude.ai 导出 candidate 加载器 ─────────────────────────────────────────

def load_claude_export_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for conversation in data:
        if not isinstance(conversation, dict) or "chat_messages" not in conversation:
            continue
        session_id = conversation.get("uuid") or path.stem
        for idx, item in enumerate(conversation.get("chat_messages") or []):
            role = role_from_sender(item.get("sender"))
            if role not in {"user", "assistant"}:
                continue
            text = text_from_export_message(item)
            if not text:
                continue
            content = item.get("content")
            img = count_images(content)
            timestamp = item.get("created_at") or item.get("updated_at") or ""
            rows.append(
                {
                    "role": role,
                    "text": text,
                    "timestamp": timestamp,
                    "session_id": session_id,
                    "source_file": str(path),
                    "source": user_source_label() if role == "user" else "opus-legacy",
                    "source_uuid": item.get("uuid") or f"export:{session_id}:{idx}",
                    "parent_uuid": item.get("parent_message_uuid") or "",
                    "has_image": 1 if img else 0,
                    "image_count": img,
                    "line_no": idx,
                    "model": "",
                    "sort_key": (timestamp, idx),
                }
            )
    return rows


def role_from_sender(sender: Any) -> str:
    if sender == "human":
        return "user"
    if sender == "assistant":
        return "assistant"
    return str(sender or "")


def text_from_export_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        return "\n".join(parts).strip()
    return str(message.get("text") or "").strip()


# ── 阶段二：去重 + 分会话编号 ───────────────────────────────────────────────

def assign_rounds(candidates: list[dict[str, Any]]) -> list[Message]:
    # 1) 按 (session_id, source_uuid) 去重，保留 sort_key 最早的一条。必须先于编号——否则
    #    JSONL 的 774 处跨文件重叠和导出两份下载的重叠会让 message_seq 重复计数。
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for c in candidates:
        key = (c["session_id"], c["source_uuid"])
        prev = best.get(key)
        if prev is None:
            best[key] = c
            continue
        _warn_inconsistent(prev, c)
        if c["sort_key"] < prev["sort_key"]:
            best[key] = c

    # 2) 分会话，按家族 sort_key 排序后编号
    by_session: dict[str, list[dict[str, Any]]] = {}
    for c in best.values():
        by_session.setdefault(c["session_id"], []).append(c)

    messages: list[Message] = []
    for items in by_session.values():
        items.sort(key=lambda c: c["sort_key"])
        round_no = 0
        seq_in_round = 0
        for c in items:
            # 每条 user 起新一轮；开头若先来 assistant，round_no==0 守卫把它归入第一轮。
            if c["role"] == "user" or round_no == 0:
                round_no += 1
                seq_in_round = 0
            seq_in_round += 1
            messages.append(_candidate_to_message(c, round_no, seq_in_round, len(messages) + 1))
    return messages


def _warn_inconsistent(a: dict[str, Any], b: dict[str, Any]) -> None:
    """同 source_uuid 的内容应字节一致（复制/重叠历史）。不一致大声报，默认保留首条。"""
    for field in ("role", "text", "timestamp", "parent_uuid"):
        if a.get(field) != b.get(field):
            print(
                f"[loaders] WARN source_uuid {a.get('source_uuid')} differs in {field!r}: "
                f"{a.get('source_file')} vs {b.get('source_file')}",
                file=sys.stderr,
            )
            return


def _candidate_to_message(c: dict[str, Any], round_no: int, message_seq: int, seq: int) -> Message:
    role = c["role"]
    return Message(
        role=role,
        speaker=user_name() if role == "user" else agent_name(),
        text=c["text"],
        timestamp=c["timestamp"],
        session_id=c["session_id"],
        seq=seq,
        round=round_no,
        label=turn_label(round_no, role),
        source_file=c.get("source_file", ""),
        source=c.get("source", "opus-legacy"),
        source_uuid=c["source_uuid"],
        parent_uuid=c.get("parent_uuid", ""),
        message_seq=message_seq,
        line_no=c.get("line_no", 0),
        has_image=c.get("has_image", 0),
        image_count=c.get("image_count", 0),
        model=c.get("model", ""),
    )


# ── normalized / test：自带显式 round，确定性 source_uuid，绕过 assign_rounds ──

def load_normalized_json(path: Path) -> list[Message]:
    return load_normalized_items(json.loads(path.read_text(encoding="utf-8")), path)


def load_normalized_items(data: Any, path: Path) -> list[Message]:
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array in {path}")
    messages: list[Message] = []
    seq_by_round: dict[int, int] = {}
    for idx, item in enumerate(data, start=1):
        role = item.get("role")
        if role not in {"user", "assistant"}:
            role = "user" if item.get("speaker") == user_name() else "assistant"
        round_no = int(item.get("round") or idx)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        timestamp = item.get("timestamp") or ""
        session_id = item.get("session_id") or path.stem
        source = item.get("source") or "opus-legacy"
        native_id = str(item.get("source_native_id") or "").strip()
        native_parent_id = str(item.get("source_parent_id") or "").strip()
        if native_id:
            # Adapter-owned native identity makes retries idempotent across spool paths.
            # Include source + session so two adapters or sessions cannot collide.
            source_uuid = _normalized_native_id(source, session_id, native_id)
            parent_uuid = (
                _normalized_native_id(source, session_id, native_parent_id)
                if native_parent_id
                else ""
            )
        else:
            # Backward-compatible path for older normalized arrays without a
            # declared adapter identity.
            source_uuid = _deterministic_id(
                "normalized", str(path), idx, role, timestamp, text
            )
            parent_uuid = ""
        seq_by_round[round_no] = seq_by_round.get(round_no, 0) + 1
        messages.append(
            Message(
                role=role,
                speaker=user_name() if role == "user" else agent_name(),
                text=text,
                timestamp=timestamp,
                session_id=session_id,
                seq=len(messages) + 1,
                round=round_no,
                label=turn_label(round_no, role),
                source_file=str(path),
                source=source,
                source_uuid=source_uuid,
                parent_uuid=parent_uuid,
                message_seq=seq_by_round[round_no],
                line_no=idx,
                model=item.get("source_model") or item.get("model") or "",
            )
        )
    return messages


def load_test_transcript(path: Path) -> list[Message]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^(\d{2})[-—]?([LC])\s*$", text))
    messages: list[Message] = []
    seq_by_round: dict[int, int] = {}
    for idx, match in enumerate(matches):
        body = text[match.end() : matches[idx + 1].start() if idx + 1 < len(matches) else len(text)].strip()
        body = re.sub(r"^(You said|Claude responded)\s*[:：]\s*", "", body, flags=re.I).strip()
        body = re.sub(r"\n?\d{1,2}:\d{2}\s*(?:AM|PM)?\s*$", "", body, flags=re.I).strip()
        if not body:
            continue
        round_no = int(match.group(1))
        role = "user" if match.group(2) == "L" else "assistant"
        seq_by_round[round_no] = seq_by_round.get(round_no, 0) + 1
        messages.append(
            Message(
                role=role,
                speaker=user_name() if role == "user" else agent_name(),
                text=body,
                timestamp="",
                session_id=path.stem,
                seq=len(messages) + 1,
                round=round_no,
                label=turn_label(round_no, role),
                source_file=str(path),
                source_uuid=_deterministic_id("test", str(path), idx, role, "", body),
                message_seq=seq_by_round[round_no],
                line_no=idx,
            )
        )
    return messages


def _deterministic_id(prefix: str, source_file: str, index: int, role: str, timestamp: str, text: str) -> str:
    digest = hashlib.sha256(f"{source_file}|{index}|{role}|{timestamp}|{text}".encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:32]}"


def _normalized_native_id(source: str, session_id: str, native_id: str) -> str:
    digest = hashlib.sha256(
        f"{source}|{session_id}|{native_id}".encode("utf-8")
    ).hexdigest()
    return f"normalized-native:{digest[:32]}"


# ── 共享文本/内容处理 ───────────────────────────────────────────────────────

MODEL_TO_SOURCE = {
    "claude-opus-4-8": "opus-4.8",
    "claude-opus-4-7": "opus-4.7",
    "claude-opus-4-6": "opus-4.6",
    "claude-opus-4-5-20250514": "opus-4.5",
    "claude-fable-5": "fable-5",
    "claude-sonnet-4-6": "sonnet-4.6",
    "claude-haiku-4-5-20251001": "haiku-4.5",
}


def model_to_source(model_id: str | None) -> str:
    if not model_id:
        return "opus-legacy"
    if model_id in MODEL_TO_SOURCE:
        return MODEL_TO_SOURCE[model_id]
    lower = model_id.lower()
    if "fable" in lower:
        return versioned_source("fable", lower)
    if "opus" in lower:
        return versioned_source("opus", lower)
    if "sonnet" in lower:
        return versioned_source("sonnet", lower)
    if "haiku" in lower:
        return versioned_source("haiku", lower)
    return model_id


def versioned_source(family: str, model_id: str) -> str:
    m = re.search(rf"{family}[-_ ]*([0-9]+)(?:[-_ ]*([0-9]+))?", model_id)
    if not m:
        return family
    major = m.group(1)
    minor = m.group(2)
    return f"{family}-{major}.{minor}" if minor else f"{family}-{major}"


_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BARE_TOKEN_RE = re.compile(r"[\w./:@-]+")


def clean_user_text(text: str) -> str:
    """剥掉混在 user 文本里的系统注入，返回用户真正说的话；判为非对话时返回空串。

    结构性的工具结果 / meta 已在 candidate 加载器按顶层字段拦掉；这里处理仍带 type=text
    的那几类：附加的 system-reminder、任务通知、result JSON、斜杠命令回显、用户中断提示。
    斜杠命令要区别对待——/goal 这类把用户的话裹在 <command-args> 里，剥壳留话；
    /model 这类配置命令 args 是裸 token（模型名/路径/开关），不是对话，丢掉。
    """
    text = _SYSTEM_REMINDER_RE.sub("", text).strip()
    if (
        not text
        or text.startswith("<task-notification>")
        or text.startswith('{"type":"result"')
        or text == "[Request interrupted by user for tool use]"
        or text == "[Request interrupted by user]"
        or (text.startswith("[heartbeat]") and "# atrun" in text)
    ):
        return ""
    # 斜杠命令的回显输出（"Set model to…"、"Goal set: …"）是命令产物不是用户的输入；
    # 真正的输入已从同一命令的 <command-args> 提取，这里整条丢掉避免重复。
    if text.startswith("<local-command-stdout>") or text.startswith("<local-command-stderr>"):
        return ""
    if text.startswith("<command-name>"):
        match = _COMMAND_ARGS_RE.search(text)
        args = match.group(1).strip() if match else ""
        if not args or _BARE_TOKEN_RE.fullmatch(args):
            return ""
        return args
    return text


def turn_label(round_no: int, role: str) -> str:
    side = "L" if role == "user" else "C"
    return f"{round_no:02d}{side}"


def text_from_content(content: Any) -> str:
    """只取 type=="text" 的 block。旧版 fallback 到 item.get("content")，把 tool_result
    的 payload（命令输出）也当正文捞了出来——这是脏数据的主源头。与导出路径口径一致。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        return "\n".join(part for part in parts if part).strip()
    return ""


def count_images(content: Any) -> int:
    if isinstance(content, list):
        return sum(1 for item in content if isinstance(item, dict) and item.get("type") == "image")
    return 0


def message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "speaker": message.speaker,
        "text": message.text,
        "timestamp": message.timestamp,
        "session_id": message.session_id,
        "seq": message.seq,
        "round": message.round,
        "message_seq": message.message_seq,
        "label": message.label,
        "line_no": message.line_no,
        "source_file": message.source_file,
        "source": message.source,
        "source_uuid": message.source_uuid,
        "parent_uuid": message.parent_uuid,
        "has_image": message.has_image,
        "image_count": message.image_count,
        "model": message.model,
    }
