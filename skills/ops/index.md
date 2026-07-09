# 运维 · Index（Leiden 索引）

> 模块文件：`src/graph.py` `src/community.py` `src/embedding.py` `src/cooccur.py` `src/entity_resolve.py`。读 `cards`/`card_tags`，写 `clusters`/`cluster_members`/`entities`/`tag_entity_map`。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 从已有 cards 重建派生索引（实体折叠 → 建图 → 递归 Leiden → 存层次）
bin/cli.py --rebuild-index
```

出卡后默认自动重建（见 `index.rebuild_after_cards`）。

## 调参（config/settings.json → `index`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `enabled` | true | 是否启用派生索引重建 |
| `algorithm` | leiden | 当前唯一正式索引算法 |
| `rebuild_after_cards` | true | 出卡后是否自动重建索引 |
| `knn_k` | 12 | card embedding kNN 候选边数量 |
| `resolve_entities` | true | 建图前是否把新 tag 折进规范实体（LLM judge，失败软降级） |

## 调参（config/settings.json → `embedding`）

embedding 服务于建图的 kNN 边，属于本模块的外部依赖。

| 参数 | 默认 | 说明 |
|------|------|------|
| `backend` | api | embedding 后端（api / ollama） |
| `model` | BAAI/bge-m3 | embedding 模型（api: BAAI/bge-m3 via SiliconFlow; ollama: nomic-embed-text） |

embedding 必需走 API（或 ollama）；实体判定的 LLM judge 可选，不可用时新 tag 留在实体边之外，不阻断索引重建。
