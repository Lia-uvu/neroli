"""Local-timezone timestamp formatting.

Leaf utility shared by the context, search, and tempo modules.
Reads the display timezone from settings.json ("timezone" key, default
Asia/Shanghai).  Callers can resolve it at the start of each operation so a
settings change takes effect without restarting a long-lived process.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from config import load_settings

LOCAL_TZ = ZoneInfo(load_settings().get("timezone", "Asia/Shanghai"))


def configured_timezone() -> ZoneInfo:
    """Return the timezone currently configured in settings.json."""
    return ZoneInfo(load_settings().get("timezone", "Asia/Shanghai"))


def format_local_timestamp(value: str | None, tz: dt.tzinfo | None = None) -> str:
    if not value:
        return ""
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(tz or configured_timezone()).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def format_local_date_time(
    value: str | None,
    tz: dt.tzinfo | None = None,
) -> tuple[str, str]:
    if not value:
        return ("", "")
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        local = parsed.astimezone(tz or configured_timezone())
        return (local.strftime("%Y-%m-%d"), local.strftime("%H:%M"))
    except ValueError:
        parts = value[:16].replace("T", " ").split(" ", 1)
        return (parts[0], parts[1] if len(parts) > 1 else "")
