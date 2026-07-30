"""Midlayer 模块（三）内部件：夜间 curator 提交校验。

同一份检查跑在两端：工作台里的 ./submit wrapper（给模型即时的 pass/fail 反馈，
拒收时告诉它差在哪、差多少）和 curator.run_curation 读 submission.json 时的复验
（真正的门——workspace-write 沙盒里模型绕过 submit 直接写文件也过不了这层）。

只用 stdlib，不 import 仓库其他文件：wrapper 在 agentic CLI 的干净环境里跑，
import 链越短越不容易在别人的机器上断。
"""
from __future__ import annotations

import math
import re

DIGEST_MAX_TOKENS = 1000
# 校验比 prompt 里告知的上限放宽 10%：本地估算和真实分词器有出入，余地给分词器
TOKEN_SLACK = 1.1
ALLOWED_KEYS = {"digest", "constants", "constants_md"}
ALLOWED_OPS = {"add", "update", "retire"}

_CJK = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")


def estimate_tokens(text: str) -> int:
    """无依赖的 token 估算：CJK 字符按 1 token 计，其余按 4 字符 1 token。
    偏保守（现代分词器常把常见中文双字并成一个 token），配合 TOKEN_SLACK 使用。"""
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return cjk + math.ceil(rest / 4)


def check(data: object, known_ids: set[str] | None = None,
          max_tokens: int = DIGEST_MAX_TOKENS) -> list[str]:
    """校验一份提交，返回错误列表（空列表 = 通过）。

    known_ids 是现有篮子的 constant_id 集合（constants.json）；传 None 跳过
    存在性检查（复验侧总是传）。错误信息面向要改稿重交的模型：说清哪条、差多少。
    """
    if not isinstance(data, dict):
        return ["提交必须是一个 JSON 对象"]
    errors = []

    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        errors.append(f"不认识的字段：{', '.join(sorted(unknown))}（只收 digest / constants / constants_md）")

    digest = data.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        errors.append("digest 缺失或为空（每晚都要交完整的更新版全文）")
    else:
        n = estimate_tokens(digest.strip())
        allowed = int(max_tokens * TOKEN_SLACK)
        if n > allowed:
            errors.append(
                f"digest 约 {n} token，上限 {max_tokens}（校验宽限到 {allowed}），"
                f"超出 {n - allowed}——压到线内再交")

    ops = data.get("constants", [])
    if not isinstance(ops, list):
        errors.append("constants 必须是数组（没有可动的就给 []）")
        ops = []
    for i, op in enumerate(ops):
        where = f"constants[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{where} 必须是对象")
            continue
        kind = op.get("op")
        if kind not in ALLOWED_OPS:
            errors.append(f"{where} 的 op 是 {kind!r}，只收 add / update / retire")
            continue
        if kind == "add":
            if not isinstance(op.get("content"), str) or not op["content"].strip():
                errors.append(f"{where}（add）缺 content")
        else:
            cid = op.get("constant_id")
            if not cid:
                errors.append(f"{where}（{kind}）缺 constant_id")
            elif known_ids is not None and cid not in known_ids:
                errors.append(f"{where} 的 constant_id {cid!r} 不在篮子里（对照 constants.json）")
            if kind == "update" and "content" not in op and "shared" not in op:
                errors.append(f"{where}（update）content 和 shared 至少给一个")

    cmd = data.get("constants_md")
    if cmd is not None and (not isinstance(cmd, str) or not cmd.strip()):
        errors.append("constants_md 给了但是空的——没有组织版就省略这个字段")

    return errors


def summary(data: dict) -> str:
    """通过后的一行回执，让模型确认提交内容和它想的一致。"""
    digest = (data.get("digest") or "").strip()
    ops = data.get("constants") or []
    counts = {k: sum(1 for o in ops if isinstance(o, dict) and o.get("op") == k)
              for k in ("add", "update", "retire")}
    parts = [f"digest 约 {estimate_tokens(digest)} token",
             f"constants add {counts['add']} / update {counts['update']} / retire {counts['retire']}"]
    if isinstance(data.get("constants_md"), str) and data["constants_md"].strip():
        parts.append("含 constants_md 组织版")
    return "，".join(parts)
