"""在线实体去重：把新卡带来的新 tag 归并到已有规范实体（增量、幂等）。

backfill（tag_wash + build_entities）只跑过一次。之后每出新卡，gen_cards 会吐新
tag——如果放任，碎片化会随时间重新长回来。本模块在 rebuild_index 建图前跑一遍：

  1. 找 card_tags 里 df>=2 但还不在 tag_entity_map 的 tag（自上次以来的新 tag）。
  2. 对每个新 tag 算 embedding，和已有实体的 name_embedding 算 cosine，取 >=0.6 候选。
  3. 候选非空 → 配置的轻量模型判"新 tag 和哪个候选是同一实体，还是都不是"（保守，
     错合比错拆代价大）。判定结果写回 tag_entity_map；判为新实体则建 entity（带 embedding）。
  4. 没候选 → 直接建新实体。

幂等：只处理未决议的 tag；已在 map 里的不重问。稳态（无新 tag）时零 LLM 调用。
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

from config import custom_cli_cmd, load_settings, model_provider
from db import create_pipeline_run, record_model_call
from embedding import get_embedding
from model import _build_codex_cmd_with_effort, build_model, model_attempts

# 阈值与裁判模型都在 settings.entity_resolve；这里的字面量只是缺省。
_er = load_settings().get("entity_resolve", {})
DEFAULT_JUDGE_MODEL = "gpt-5.6-luna"
JUDGE_MODEL = _er.get("judge_model", DEFAULT_JUDGE_MODEL)
COSINE_MIN = _er.get("cosine_min", 0.6)   # candidate threshold (design Stage 2)
TOP_K = _er.get("top_k", 5)               # max candidates shown to the judge
MIN_DF = _er.get("min_df", 2)             # lazy promotion: only resolve tags that have recurred
BATCH = _er.get("batch", 12)              # new tags per codex call

PROMPT_HEADER = """你在帮个人记忆系统做实体去重。下面每条是一个【新标签】和它的【候选已有实体】（cosine 初筛）。
判断新标签和哪个候选指向【同一个实体】，应合并到那个候选；如果都不是同一实体，则它是新实体。

规则：只有真正同义/同一所指才合并；上位词≠下位词（求职≠求职焦虑）；相关≠相同（简历≠求职）；
不同物种/对象不合并。不确定就判新实体。错误合并比错误新建代价大。

只输出 JSON 数组，每条一个元素：{"tag":"新标签","merge_into":"某个候选 或 null"}，不要解释。

待判：
"""


def _blob(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unblob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _embed_settings() -> tuple[str, str]:
    es = load_settings().get("embedding", {})
    return es.get("backend", "api"), es.get("model", "")


def populate_embeddings(conn) -> int:
    """Fill name_embedding for entities missing it. Returns count embedded."""
    backend, model = _embed_settings()
    rows = conn.execute(
        "SELECT entity_id, canonical_name FROM entities WHERE name_embedding IS NULL"
    ).fetchall()
    for r in rows:
        vec = get_embedding(r["canonical_name"], backend, model)
        conn.execute(
            "UPDATE entities SET name_embedding = ? WHERE entity_id = ?",
            (_blob(vec), r["entity_id"]),
        )
    conn.commit()
    return len(rows)


def _entity_matrix(conn):
    rows = conn.execute(
        "SELECT entity_id, canonical_name, name_embedding FROM entities WHERE name_embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return [], [], None
    ids = [r["entity_id"] for r in rows]
    names = [r["canonical_name"] for r in rows]
    mat = np.vstack([_unblob(r["name_embedding"]) for r in rows]).astype(np.float32)
    mat /= np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)
    return ids, names, mat


def _unresolved_tags(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT ct.tag AS tag, COUNT(DISTINCT ct.card_id) AS df
        FROM card_tags ct
        LEFT JOIN tag_entity_map m ON m.tag = ct.tag
        WHERE m.tag IS NULL
        GROUP BY ct.tag HAVING df >= ?
        """,
        (MIN_DF,),
    ).fetchall()
    return [r["tag"] for r in rows]


def _parse_judge_output(out: str, items: list[tuple[str, list[str]]]) -> dict[str, str | None]:
    """严格解析一个完整 batch；缺项/重复/非法候选都视为失败，不静默建新实体。"""
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        raise RuntimeError("entity judge output did not contain a JSON array")
    try:
        verdicts = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError("entity judge output contained invalid JSON") from exc
    if not isinstance(verdicts, list):
        raise RuntimeError("entity judge verdict must be a JSON array")
    result: dict[str, str | None] = {}
    valid = {tag: set(cands) for tag, cands in items}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise RuntimeError("entity judge verdict item must be an object")
        tag = verdict.get("tag")
        into = verdict.get("merge_into")
        if tag not in valid or tag in result:
            raise RuntimeError(f"entity judge returned unknown/duplicate tag: {tag!r}")
        if into is not None and into not in valid[tag]:
            raise RuntimeError(f"entity judge returned invalid candidate for {tag!r}: {into!r}")
        result[tag] = into
    missing = set(valid) - set(result)
    if missing:
        raise RuntimeError("entity judge omitted tags: " + ", ".join(sorted(missing)))
    return result


def _judge(judge, items: list[tuple[str, list[str]]], conn, run_id: str) -> dict[str, str | None]:
    """items: (new_tag, [candidate names]); returns new_tag -> chosen candidate or None."""
    lines = [f'{i+1}. "{tag}" 候选: {", ".join(cands)}' for i, (tag, cands) in enumerate(items)]
    prompt = PROMPT_HEADER + "\n".join(lines)
    out = ""
    error = None
    try:
        out = judge.run(prompt)
        return _parse_judge_output(out, items)
    except Exception as exc:
        error = exc
        raise
    finally:
        attempts = model_attempts(judge) or [{
            "attempt": 1, "status": "error" if error else "success",
            "returncode": None, "stdout": out, "stderr": str(error) if error else "",
        }]
        for attempt in attempts:
            stdout = str(attempt.get("stdout") or "")
            stderr = str(attempt.get("stderr") or "")
            meta = {k: v for k, v in attempt.items() if k not in ("stdout", "stderr")}
            record_model_call(
                conn, run_id,
                "entity_resolve_attempt_error" if error else "entity_resolve_attempt",
                prompt, stdout or (f"(error: {stderr or error})" if error or stderr else ""),
                meta,
            )
        conn.commit()


def _new_entity(conn, name: str, backend: str, model: str) -> int:
    vec = get_embedding(name, backend, model)
    cur = conn.execute(
        "INSERT INTO entities (canonical_name, name_embedding) VALUES (?, ?)",
        (name, _blob(vec)),
    )
    return cur.lastrowid


def resolve_new_tags(conn) -> dict:
    """Resolve df>=2 tags not yet mapped. Idempotent; zero LLM calls when none."""
    populate_embeddings(conn)
    new_tags = _unresolved_tags(conn)
    if not new_tags:
        return {"new_tags": 0, "merged": 0, "created": 0}

    backend, model = _embed_settings()
    ids, names, mat = _entity_matrix(conn)
    name_to_id = dict(zip(names, ids))

    # cosine candidates per new tag
    pending: list[tuple[str, list[str]]] = []
    no_candidate: list[str] = []
    for tag in new_tags:
        vec = np.asarray(get_embedding(tag, backend, model), dtype=np.float32)
        if mat is None:
            no_candidate.append(tag)
            continue
        vec /= max(float(np.linalg.norm(vec)), 1e-12)
        sims = mat @ vec
        order = np.argsort(-sims)[:TOP_K]
        cands = [names[i] for i in order if sims[i] >= COSINE_MIN]
        (pending.append((tag, cands)) if cands else no_candidate.append(tag))

    merged = created = 0

    # tags with no cosine candidate -> straight new entities
    for tag in no_candidate:
        eid = _new_entity(conn, tag, backend, model)
        conn.execute("INSERT INTO tag_entity_map (tag, entity_id) VALUES (?, ?)", (tag, eid))
        name_to_id[tag] = eid
        created += 1

    # tags with candidates -> LLM adjudicates, batched
    if pending:
        # 裁判走全局调用方式（settings.model_access）：cli 默认自建 codex，api/ollama 直连。
        prov = model_provider()
        cmd = None
        if prov == "cli":
            cmd = custom_cli_cmd(JUDGE_MODEL) or _build_codex_cmd_with_effort(JUDGE_MODEL, "low")
        judge = build_model(prov, JUDGE_MODEL,
                            api_base_url=os.environ.get("CLAUDE_MEMORY_API_BASE_URL"),
                            model_cmd=cmd)
        run_id = create_pipeline_run(conn, judge.name, None, [])
        for start in range(0, len(pending), BATCH):
            chunk = pending[start:start + BATCH]
            verdict = _judge(judge, chunk, conn, run_id)
            for tag, cands in chunk:
                into = verdict.get(tag)
                if into and into in name_to_id:
                    conn.execute(
                        "INSERT INTO tag_entity_map (tag, entity_id) VALUES (?, ?)",
                        (tag, name_to_id[into]),
                    )
                    merged += 1
                else:
                    eid = _new_entity(conn, tag, backend, model)
                    conn.execute("INSERT INTO tag_entity_map (tag, entity_id) VALUES (?, ?)", (tag, eid))
                    name_to_id[tag] = eid
                    created += 1

    conn.commit()
    return {"new_tags": len(new_tags), "merged": merged, "created": created}


if __name__ == "__main__":
    from db import connect
    print(resolve_new_tags(connect()))
