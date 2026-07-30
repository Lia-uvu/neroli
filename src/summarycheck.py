"""Validation shared by the last-24 workbench submit tool and final recheck."""
from __future__ import annotations

SUMMARY_MAX_CHARS = 700
ALLOWED_KEYS = {"summary"}


def check(data: object, max_chars: int = SUMMARY_MAX_CHARS) -> list[str]:
    if not isinstance(data, dict):
        return ["提交必须是一个 JSON 对象"]
    errors: list[str] = []
    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        errors.append(f"不认识的字段：{', '.join(sorted(unknown))}（只收 summary）")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary 缺失或为空")
    elif len(summary.strip()) > max_chars:
        n = len(summary.strip())
        errors.append(f"summary 共 {n} 字，上限 {max_chars}，超出 {n - max_chars} 字")
    return errors


def summary(data: dict) -> str:
    body = str(data.get("summary") or "").strip()
    return f"summary {len(body)} 字"
