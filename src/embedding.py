"""卡片文本 → 向量 + 共享词表（中性工具，无聚类算法依赖）。

原本这些 helper 住在 clustering.py（crel 聚类模块）里，v2 索引层（graph.py /
cooccur.py / tag_wash.py）只借用其中的 embedding / 文本 / STOP 部分。crel 退役后
把这些中性 helper 抽到这里，clustering.py 整体删除。

  - get_embedding + 磁盘缓存：卡片正文 / tag 文本 → 向量（ollama 或 api 后端）。
  - card_text / card_entities：从卡片行取正文 / 实体标签。
  - cosine / mean_vec / center_embeddings：向量运算 + mean-centering。
  - GENERIC / STOP：建图前过滤的背景词 / 情绪词（词表在 config，settings 可调）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

from config import MEMORY, generic_tags, stop_tags

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
if VENDOR.exists() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

CACHE_DIR = MEMORY / "data" / ".emb_cache"
CACHE_DIR.mkdir(exist_ok=True)


# 词表来源统一在 config：内置虚词 + 称呼别名（settings.user/agent）+ settings.vocab。
# 称呼必须在这里被屏蔽——它和一切共现，进了建图/共现会污染聚类。
GENERIC = generic_tags()
STOP = stop_tags()


def card_text(c: dict | sqlite3.Row) -> str:
    parts = [c["headline"] or "", c["share"] or "", c["private"] or ""]
    return "\n".join(p for p in parts if p).strip()


def card_entities(c: dict | sqlite3.Row) -> list[str]:
    if isinstance(c, sqlite3.Row):
        return []
    ents = [t.strip().lower() for t in (c.get("tags") or []) if t.strip()]
    return [e for e in ents if e and e not in GENERIC]


def _env_from_dotenv():
    env_file = MEMORY / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_env_from_dotenv()


# ---- Embedding ----

def _cache_key(backend: str, model: str, text: str) -> Path:
    h = hashlib.sha1(f"{backend}|{model}|{text}".encode()).hexdigest()
    return CACHE_DIR / f"{backend}_{h}.json"


def _embed_ollama(text: str, model: str = "nomic-embed-text") -> list[float]:
    from config import load_settings
    base = load_settings().get("embedding", {}).get("ollama_url", "http://localhost:11434").rstrip("/")
    payload = text if ":" in text[:20] else f"search_document: {text}"
    req = urllib.request.Request(
        f"{base}/api/embeddings",
        data=json.dumps({"model": model, "prompt": payload}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["embedding"]


def _embed_api(text: str, model: str = "BAAI/bge-m3") -> list[float]:
    base = os.environ["CLAUDE_MEMORY_API_BASE_URL"].rstrip("/")
    key = os.environ["CLAUDE_MEMORY_API_KEY"]
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps({"model": model, "input": text}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["data"][0]["embedding"]


def get_embedding(text: str, backend: str = "ollama", model: str = "") -> list[float]:
    if not model:
        model = "nomic-embed-text" if backend == "ollama" else "BAAI/bge-m3"
    ck = _cache_key(backend, model, text)
    if ck.exists():
        return json.loads(ck.read_text())
    vec = _embed_ollama(text, model) if backend == "ollama" else _embed_api(text, model)
    ck.write_text(json.dumps(vec))
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def mean_vec(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


def center_embeddings(embs: dict[str, list[float]]) -> dict[str, list[float]]:
    vecs = list(embs.values())
    mean = mean_vec(vecs)
    return {k: [x - m for x, m in zip(v, mean)] for k, v in embs.items()}
