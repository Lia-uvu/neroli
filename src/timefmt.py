"""Local-timezone timestamp formatting.

Leaf utility shared by the context, search, and tempo modules.
Reads the display timezone from settings.json ("timezone" key, default
Asia/Shanghai).  The value is resolved once at import time so every
module that touches LOCAL_TZ sees the same zone.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from config import load_settings

LOCAL_TZ = ZoneInfo(load_settings().get("timezone", "Asia/Shanghai"))


def format_local_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def format_local_date_time(value: str | None) -> tuple[str, str]:
    if not value:
        return ("", "")
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        local = parsed.astimezone(LOCAL_TZ)
        return (local.strftime("%Y-%m-%d"), local.strftime("%H:%M"))
    except ValueError:
        parts = value[:16].replace("T", " ").split(" ", 1)
        return (parts[0], parts[1] if len(parts) > 1 else "")
