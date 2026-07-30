"""Agent-written recent summary built from cards-last-24 plus filtered recall."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import stat
import sqlite3
from pathlib import Path
from typing import Callable

import curator
import summarycheck
from config import MEMORY, ROOM_DIRS, ROOMS, load_settings
from context import cards_path_for
from db import DB, card_visible_clause, create_pipeline_run, record_model_call
from model import ModelRunner, model_attempts
from timefmt import configured_timezone

WORKBENCH_DIR = MEMORY / "data" / "last24-summary"
PROMPT_FILE = MEMORY / "prompts" / "last24-summary.md"

_SUBMIT_TEMPLATE = '''#!/usr/bin/env python3
"""Submit a last-24 summary for validation and final recheck."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, {src!r})
import summarycheck  # noqa: E402


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        text = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    try:
        data = json.loads(text)
    except ValueError as error:
        print(f"REJECTED: 不是合法 JSON（{{error}}）")
        return 1
    errors = summarycheck.check(data, max_chars={max_chars})
    if errors:
        print("REJECTED:")
        for error in errors:
            print(f"- {{error}}")
        return 1
    (HERE / "submission.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(f"PASS: {{summarycheck.summary(data)}}。已提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def summary_settings() -> dict:
    return load_settings().get("last24_summary", {})


def max_chars() -> int:
    return int(summary_settings().get("max_chars", 700))


def summary_agent_name() -> str:
    return str(summary_settings().get("agent_name", "Assistant"))


def summary_timezone_name() -> str:
    return str(load_settings().get("timezone", "UTC"))


def summary_now_label() -> str:
    return dt.datetime.now(configured_timezone()).strftime("%Y-%m-%d %H:%M")


def enabled_rooms(viewer: str | None = None) -> list[str]:
    settings = summary_settings()
    if not settings.get("enabled", True):
        return []
    rooms = [viewer] if viewer else list(ROOMS)
    overrides = settings.get("rooms", {})
    return [room for room in rooms if overrides.get(room, True)]


def summary_path_for(room: str) -> Path:
    if room not in ROOM_DIRS:
        raise ValueError(f"unknown room for summary output: {room!r}")
    return ROOM_DIRS[room] / "summary-last-24.md"


def recent_card_count(conn: sqlite3.Connection, room: str) -> int:
    lookback = load_settings().get("context", {}).get("lookback_hours", 24)
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=lookback)).isoformat()
    visible, params = card_visible_clause(room, "cards")
    return conn.execute(
        f"SELECT COUNT(*) FROM cards WHERE timestamp >= ? AND {visible}",
        (cutoff, *params),
    ).fetchone()[0]


def export_workbench(conn: sqlite3.Connection, room: str, db_path: Path = DB) -> Path:
    dest = WORKBENCH_DIR / room
    dest.mkdir(parents=True, exist_ok=True)
    curator._export_view_db(conn, room, dest / "view.db", db_path)
    shutil.copy2(cards_path_for(room), dest / "cards-last-24.md")
    tools = {
        "recall": curator._RECALL_TEMPLATE.format(src=str(curator.SRC_DIR.resolve())),
        "submit": _SUBMIT_TEMPLATE.format(
            src=str(curator.SRC_DIR.resolve()), max_chars=max_chars()),
    }
    for name, text in tools.items():
        path = dest / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (dest / "submission.json").unlink(missing_ok=True)
    return dest


def fill_prompt(room: str) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    return (template.replace("{room}", room)
            .replace("{summary-agent}", summary_agent_name())
            .replace("{timezone}", summary_timezone_name())
            .replace("{now}", summary_now_label())
            .replace("{summary-max}", str(max_chars())))


def run_summaries(
    conn: sqlite3.Connection,
    model_factory: Callable[[str], ModelRunner],
    viewer: str | None = None,
    db_path: Path = DB,
) -> list[dict]:
    rooms = enabled_rooms(viewer)
    run_id = create_pipeline_run(conn, "last24-summary", PROMPT_FILE, [])
    results: list[dict] = []
    for room in rooms:
        if recent_card_count(conn, room) == 0:
            body = "最近24小时没有可见的新事件卡。"
            _write_summary(room, body)
            results.append({"room": room, "summary_chars": len(body), "model": False})
            continue
        dest = export_workbench(conn, room, db_path)
        prompt = fill_prompt(room)
        model = model_factory(str(dest))
        try:
            raw = model.run(prompt)
        except Exception as error:
            _record_attempts(conn, run_id, room, prompt, model)
            parsed = _read_submission(dest)
            if parsed is None:
                record_model_call(
                    conn, run_id, f"last24:{room}:error", prompt,
                    f"(error: {error})", {"error": str(error)}, session_id=room)
                conn.commit()
                raise
            raw = f"(stdout empty, recovered from submission.json: {error})"
            submitted = True
        else:
            _record_attempts(conn, run_id, room, prompt, model)
            parsed = _read_submission(dest)
            submitted = parsed is not None
            if parsed is None:
                parsed = _parse_output(raw)
                errors = summarycheck.check(parsed, max_chars=max_chars())
                if errors:
                    record_model_call(
                        conn, run_id, f"last24:{room}:error", prompt, raw,
                        {"errors": errors, "parsed": parsed}, session_id=room)
                    conn.commit()
                    raise RuntimeError(
                        "last24 summary produced no valid submission: " + "; ".join(errors))
        record_model_call(
            conn, run_id, f"last24:{room}", prompt, raw, parsed, session_id=room)
        body = parsed["summary"].strip()
        _write_summary(room, body)
        conn.commit()
        results.append({
            "room": room, "summary_chars": len(body),
            "model": True, "submitted": submitted,
        })
    return results


def _read_submission(dest: Path) -> dict | None:
    path = dest / "submission.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if not summarycheck.check(data, max_chars=max_chars()) else None


def _parse_output(text: str) -> dict:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = match.group(1) if match else None
    if blob is None:
        start, end = text.find("{"), text.rfind("}")
        blob = text[start:end + 1] if start != -1 and end > start else ""
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _record_attempts(conn: sqlite3.Connection, run_id: str, room: str,
                     prompt: str, model: ModelRunner) -> None:
    for attempt in model_attempts(model):
        stdout = str(attempt.get("stdout") or "")
        stderr = str(attempt.get("stderr") or "")
        meta = {k: v for k, v in attempt.items() if k not in ("stdout", "stderr")}
        record_model_call(
            conn, run_id, f"last24:{room}:attempt", prompt,
            stdout or (f"(error: {stderr})" if stderr else ""), meta,
            session_id=room)
    conn.commit()


def _write_summary(room: str, body: str) -> None:
    now = dt.datetime.now(configured_timezone()).strftime("%Y-%m-%d %H:%M")
    path = summary_path_for(room)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 最近24小时小结（更新至 {now}）\n\n"
        f"> {summary_agent_name()} 根据事件卡整理；需要时可用 recall 核实；"
        "结果通过提交门复验\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
