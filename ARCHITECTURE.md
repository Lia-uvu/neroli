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

### 七个模块

| 模块 | 文件 | 读 | 写 | 需要模型 | 运维 |
|------|------|----|----|---------|------|
| **Ingest** | `loaders.py` (+`memory_types.py`) | 外部对话文件 | `source_sessions` `messages` `turns` `conversation_nodes` `conversation_observations` + Card branch projection | 否 | [ops/ingest](skills/ops/ingest.md) |
| **Card Gen** | `gen_cards.py` (+`model.py`) | `turns` `messages` | `cards` `card_tags` `card_raw` `cards_fts` `*_runs/calls` | 是 | [ops/card-gen](skills/ops/card-gen.md) |
| **History** | `history.py` | `conversation_nodes` `conversation_observations` | source-neutral JSON read projection | 否 | [ops/ingest](skills/ops/ingest.md) |
| **Index** | `graph.py` `community.py` `embedding.py` `cooccur.py` `entity_resolve.py` | `cards` `card_tags` | `clusters` `cluster_members` `entities` `tag_entity_map` | embedding 必需走 API；实体判定 LLM judge 可选（失败软降级） | [ops/index](skills/ops/index.md) |
| **Context** | `context.py` `tempo.py` | `cards` `turns` | `<room>/cards-last-24.md`（文件） | 否 | [ops/context](skills/ops/context.md) |
| **Search** | `retrieval.py` | 所有表（只读） | — | 否 | [ops/search](skills/ops/search.md) |
| **Midlayer** | `last24.py` `summarycheck.py` `treesnap.py` `curator.py` `submitcheck.py` | `clusters` `cluster_members` `cards` `card_tags` `tag_entity_map` `entities` | `<room>/summary-last-24.md` + `tree_snapshots` `tree_snapshot_members` `digests` `constants` + 房间文件 | last24/curator agent 是；treesnap 否 | [ops/midlayer](skills/ops/midlayer.md) |

### 白天运行时边界

Tree-aware adapter 把完整 `node + parent` 原文树和可选 observation 交给 Ingest。Neroli
保存 `conversation_nodes` 正本，将对话节点投影到统一 `messages` / `turns` 供 Card Gen。
observation 只写 `conversation_observations`，History 用它还原 runtime 当时指向的路径；它不写
`turns`、不进入 Card 规划。SQLite 在
`turns` 的实际 INSERT / UPDATE / DELETE 上维护 `change_watermarks.turns`；独立的
`bin/watch-cards.sh` 轮询这个数据库水位并调用编排层已有的 `--auto-cards`。因此 Card Gen
不依赖 Claude Code 的 fswatch，也不要求每个未来 adapter 额外 import 或调用 Card Gen。
nightly 是独立的冷 session 补漏、index、last24、curator 流程，不承担白天唤醒职责。

新的 tree-aware adapter 使用 [`neroli-conversation-tree-v1`](docs/conversation-tree-adapter-contract.md)：
adapter 交完整树和它明确服务的 room，Neroli 校验本机 `ingest.source_rooms` 授权。
[`neroli-normalized-v2`](docs/normalized-adapter-contract.md) 作为线性 adapter 的兼容契约继续保留：adapter 交
native session/message identity、不可变原文、UTC 发生时间和稳定来源顺序；Neroli 生成
canonical ID 与 round。adapter 的 `source_route` 只描述入口，本机私有
`ingest.source_routes` policy 才能把它绑定到 room；未知 route 拒绝入库，不回退默认房间。
adapter 与 Neroli 各自负责什么、当前 parent-linked tree 能表达什么、哪些 deletion
语义仍未实现，见 [`source adapter boundary`](docs/source-adapter-boundary.md)。

### 边界规则

- 模块只能依赖：**存储层**（`db`）、**叶子工具**（`config`/`timefmt`/`memory_types`/`model`）、**本模块内部文件**（如 Index 内 community→graph→embedding）。
- 模块**之间不互相 import**。要交换数据走数据库表。
- 只有**编排层**（`cli`/`pipeline`）可以 import 各模块来串联流程。
- 改动时守住这条线：新代码若让一个模块去 import 另一个模块，多半是逻辑放错了层。
- **卡可见性谓词**（room viewer 隐私规则）住在存储层 `db.card_visible_clause`，Search / Context /
  Midlayer 共用同一条——隐私规则今后只改这一处。
- Midlayer 内部 `curator → treesnap` 属本模块内依赖（允许）。curator 导出到工作台的 `recall`
  wrapper 是独立入口脚本、在工作台里 import Search 门面（`retrieval`），属工具面而非模块跨 import。

### 测试地图

测试跟随其拥有的模块；`contracts/` 只验证 Neroli 自己的公开数据库边界，不引用具体
source adapter 的实现或私人目录。

```text
tests/
├── ingest/       loader 与增量摄入
├── card_gen/     出卡解析、失败安全与 session eligibility
├── index/        图索引与实体解析
├── search/       检索、可见性与相邻卡
├── midlayer/     last24、tree snapshot、curator 与 submission gate
├── runtime/      lock、model runner 与叶子工具
└── contracts/    来源独立、tree/observation、turns 水位与跨模块 smoke contract
```

完整测试：

```sh
python3 -m unittest discover -s tests -v
```

---

设计原理、架构决策、废弃方案的详细讲解文档整理中，尚未随仓库发布。

运维速查 → Markdown（按模块拆分，查啥读啥）：

- **[skills/ops/SKILL.md](skills/ops/SKILL.md)** — 运维 Skill：按模块导航 + 「改哪里」表
- **[skills/ops/](skills/ops/)** — 各模块运维 Skill：ingest / card-gen / index / context / search / common
- **[schema.md](schema.md)** — SQLite v13 表结构（conversation tree + observations + overlapping Card membership + 兼容与派生层）
