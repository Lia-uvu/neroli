# recall-pipeline 文档

## 模块地图

**接口契约 = SQLite schema。** 模块之间不互相 import，只通过数据库表读写交换数据；
顶层编排层（`cli` / `pipeline`）负责把模块串起来。任一模块可被替换或停用，
其余照常运作（cards 是核心数据，不可省略，但实现可换）。

### 分层

| 层 | 文件 | 职责 |
|----|------|------|
| 叶子工具 | `config.py` `memory_types.py` `model.py` `timefmt.py` | 无本地依赖，谁都能 import |
| 存储 | `db.py` | 纯 SQLite：连接、ingest 写入、卡增删查、FTS、审计、fork |
| 模块 | 见下表 | 各自只读写自己负责的表 |
| 编排 | `cli.py` `pipeline.py` | 解析命令 / 串联模块 |

### 六个模块

| 模块 | 文件 | 读 | 写 | 需要模型 | 运维 |
|------|------|----|----|---------|------|
| **Ingest** | `loaders.py` (+`memory_types.py`) | 外部对话文件 | `messages` `turns` | 否 | [ops/ingest](skills/ops/ingest.md) |
| **Card Gen** | `gen_cards.py` (+`model.py`) | `turns` `messages` | `cards` `card_tags` `card_raw` `cards_fts` `*_runs/calls` | 是 | [ops/card-gen](skills/ops/card-gen.md) |
| **Index** | `graph.py` `community.py` `embedding.py` `cooccur.py` `entity_resolve.py` | `cards` `card_tags` | `clusters` `cluster_members` `entities` `tag_entity_map` | embedding 必需走 API；实体判定 LLM judge 可选（失败软降级） | [ops/index](skills/ops/index.md) |
| **Context** | `context.py` `tempo.py` | `cards` `turns` | `<room>/context-last-24.md`（文件） | 否 | [ops/context](skills/ops/context.md) |
| **Search** | `retrieval.py` | 所有表（只读） | — | 否 | [ops/search](skills/ops/search.md) |
| **Midlayer** | `treesnap.py` `curator.py` `submitcheck.py` | `clusters` `cluster_members` `cards` `card_tags` `tag_entity_map` `entities` | `tree_snapshots` `tree_snapshot_members` `digests` `constants` + 房间文件 | treesnap 否；curator agent（阶段3）是 | [ops/midlayer](skills/ops/midlayer.md) |

### 边界规则

- 模块只能依赖：**存储层**（`db`）、**叶子工具**（`config`/`timefmt`/`memory_types`/`model`）、**本模块内部文件**（如 Index 内 community→graph→embedding）。
- 模块**之间不互相 import**。要交换数据走数据库表。
- 只有**编排层**（`cli`/`pipeline`）可以 import 各模块来串联流程。
- 改动时守住这条线：新代码若让一个模块去 import 另一个模块，多半是逻辑放错了层。
- **卡可见性谓词**（room viewer 隐私规则）住在存储层 `db.card_visible_clause`，Search / Context /
  Midlayer 共用同一条——隐私规则今后只改这一处。
- Midlayer 内部 `curator → treesnap` 属本模块内依赖（允许）。curator 导出到工作台的 `recall`
  wrapper 是独立入口脚本、在工作台里 import Search 门面（`retrieval`），属工具面而非模块跨 import。

---

设计原理、架构决策、废弃方案的详细讲解文档整理中，尚未随仓库发布。

运维速查 → Markdown（按模块拆分，查啥读啥）：

- **[skills/ops/SKILL.md](skills/ops/SKILL.md)** — 运维 Skill：按模块导航 + 「改哪里」表
- **[skills/ops/](skills/ops/)** — 各模块运维 Skill：ingest / card-gen / index / context / search / common
- **[schema.md](schema.md)** — SQLite v7 表结构（messages/turns + Leiden index + midlayer）
