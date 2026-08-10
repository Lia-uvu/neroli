# Neroli 表结构（schema v15）

> 改 db.py 或 schema.sql 时查这个。
>
> **版本机制**：`PRAGMA user_version = 13`。connect() 只校验版本不做迁移；
> 升级写 `migrations/NNN-*.sql`（手动 `sqlite3 db < 迁移文件`），并同步 schema.sql 和本文档。

## 原始层（v4：内容与发生分离）

v3 的单表 `turns` 有数据完整性 bug：`UNIQUE(session_id, round, role)` 让一轮内多条 assistant
互相覆盖；无去重键让跨会话复制的历史无法折叠；`--max-messages` 截断 + per-file 重算 round 造成
轮次漂移。v4 把原始层拆成两张：

- **messages** = 去重后的规范内容，`source_uuid` 为全局去重键，内容首次写入即定、不可变。
- **turns** = 每个会话内的一次出现 + 排序。同一 uuid 在 N 个会话 = N 行 turns，1 行 messages。

会话重建 = `turns JOIN messages ON source_uuid`，按 `(round, message_seq, line_no)` 排序。

### 三个 source 家族

| 家族 | 文件 | source_uuid | session_id | 排序 | 生命周期 |
|------|------|-------------|------------|------|----------|
| Claude Code JSONL | `~/.claude/projects/<room>/*.jsonl` | 顶层 `uuid` | 顶层 `sessionId` | 文件追加序（多文件→最早ts再 line_no） | **live**，watcher 实时 tail 已配置的 room |
| Claude.ai 导出 | `backups/origin-data/**/conversations.json` | `chat_messages[].uuid`（原生） | 会话 `uuid` | 消息 `created_at` + 数组下标 | **archive**，一次性手动 backfill，不 watch |
| normalized adapter v2 | `*.json` envelope | `sha256(source + native_message_id)` | `sha256(source + native_session_id)` | 按 `source_sequence` 排序，再由 Neroli 推导 round | adapter snapshot / live spool |
| conversation tree v1 | `*.json` envelope | `conversation_nodes.node_id` 的 message 投影 | Card-only branch session | 每条树路径临时推导；不是 source fact | incremental node batch |
| legacy normalized / test | `*.json` array / `*.txt` | 旧 source+session+native id 或 path/content 哈希 | 显式 / `path.stem` | adapter 显式 round | compatibility / dev |

加载分两阶段：candidate 加载器抽字段带 sort_key（不定 round）→ `assign_rounds` 对全量先按
`(session_id, source_uuid)` 去重再分会话编号。入口 `load_messages_for_ingest(paths)` 按家族路由。
ingest 必须读完整文件（round 跨文件一次算），`--max-messages` 仅供 `--dump-json` 查看。
v2 完整字段、ID 公式、不可变与路由规则见
[`docs/normalized-adapter-contract.md`](docs/normalized-adapter-contract.md)。
adapter / Neroli 职责分界、当前 tree 表达能力和未实现的 deletion 语义见
[`docs/source-adapter-boundary.md`](docs/source-adapter-boundary.md)。
基于 Pi / Claude Code 原生节点、parent edge 与可选 observation 的 contract
见 [`docs/conversation-tree-adapter-contract.md`](docs/conversation-tree-adapter-contract.md)；
schema v12 已实现。

## source_sessions（adapter session provenance，v11）

仅 `neroli-normalized-v2` session 写此表；旧来源继续从 `turns.source_file` 派生 room。

| 字段 | 说明 |
|------|------|
| session_id | Neroli canonical session ID，PK |
| source / native_session_id | adapter namespace 与原生 session identity；组合唯一 |
| native_parent_session_id / parent_session_id | 原生与 canonical 父 session，可为空 |
| source_route | adapter 的稳定入口标签，不是 room |
| room | 本机私有 `ingest.source_routes` policy 的解析结果 |
| created_at / updated_at | 首次与最近一次接收时间 |

## conversation_nodes（来源中立原文树，v12）

Tree-aware adapter 的 canonical 正本。节点以 `(source, room, native_node_id)` 命名，
一个节点最多一个 parent；同 ID 的内容、parent 和 provenance 不可变。

| 字段 | 说明 |
|------|------|
| node_id | Neroli canonical ID，PK |
| source / room / native_node_id | adapter namespace、明确房间来源与原生节点 ID；组合唯一 |
| parent_node_id / native_parent_node_id | canonical / 原生 parent；根为 NULL |
| occurred_at | 来源发生时间，可空 |
| kind / source_type | portable 类型（message/tool/checkpoint/event）与来源类型 |
| role / text / message_json | 只在 portable user/assistant message 上存在 |
| provider / model | 来源可权威提供时保留 |

## conversation_observations（渲染旁注，v12）

可选、只追加的 source observation。当前 `cursor` 记录“在某 runtime context 与
observed_at，指针在哪个节点”。History 投影可沿 parent 还原当时路径；Card Gen
不读此表，写此表也不推进 `turns` 水位。

| 字段 | 说明 |
|------|------|
| observation_id | 完整规范事实的确定性哈希，PK |
| source / room / native_context_id | 来源、房间和 runtime/session context |
| kind / observed_at | 开放 observation 类型与 UTC 观测时间 |
| node_id / native_node_id | 指向同一 forest 内的节点 |
| payload_json | adapter 可选 JSON 旁注；Neroli 不解释未知语义 |

## conversation_card_branches / conversation_node_branches（Card 派生处理状态，v12）

把原文树确定性投影成现有 rolling Card Planner 可读的 session 流。第一个被发现
的 child 延续已有处理流，后续 sibling 建 peer 流；共享 ancestor 在 peer 的 `turns`
里标 `is_context=1`。这是可重建的 coverage/ownership 状态，不是 canonical main/active
branch 属性。

## messages（去重后的规范内容）

| 字段 | 说明 |
|------|------|
| source_uuid | PK，全局去重键（原生 uuid 或确定性哈希） |
| role | user / assistant |
| speaker | User / Assistant |
| text | 消息正文（只取 type==text 的 block） |
| timestamp | ISO 8601 UTC，唯一时间真相，流入 cards.timestamp |
| parent_uuid | 父消息 uuid（重建会话树用） |
| source | v2 为 adapter namespace；旧来源为 user / model-alias 等历史标签。空间归属不由此字段直接决定 |
| provider | v2 adapter 提交的原始 provider，可空 |
| model | 官方 model id 原样 |
| native_message_id / native_parent_message_id | v2 adapter 的原生不可变 identity/provenance |
| has_image / image_count | 该消息含图片 block 数 |
| created_at | 入库时间 |

## turns（每会话出现 + 排序）

| 字段 | 说明 |
|------|------|
| id | 自增 PK |
| session_id | 会话标识 |
| source_uuid | FK → messages |
| round | 会话内交换序号（每条 user 起新轮） |
| message_seq | 轮内位置（user=1，assistant=2..N） |
| source_file | 来源文件路径 |
| line_no | 文件追加序 / 会话内数组下标 |
| is_context | tree Card 投影中 1=共享 ancestor 只读语境，0=本 branch 新物料；legacy 默认 0 |
| created_at | **INGEST 时间**（非对话时间）；recency 一律用 messages.timestamp |

`UNIQUE(session_id, source_uuid)` — 同一 uuid 在一个会话只一行；全量重读用 DO UPDATE 自纠 round/seq。

## change_watermarks（原始层变更水位，v10）

| 字段 | 说明 |
|------|------|
| name | 水位名；当前固定为 `turns` |
| revision | `turns` 实际 INSERT / UPDATE / DELETE 时单调递增 |
| updated_at | 最近一次水位变化的数据库时间 |

SQLite triggers 维护水位。幂等 ingest 的 `ON CONFLICT DO UPDATE` 若业务字段没有实际变化，不递增；
cards、index、context 等派生层写入也不递增。白天 `bin/watch-cards.sh` 只轮询这个水位，因此
Claude Code、Claude.ai/API 或未来 adapter 无论从哪里写入，只要最终改变 `turns`，都会走同一个
Card Gen 唤醒入口。

## cards（事件卡，派生层核心）

| 字段 | 说明 |
|------|------|
| card_id | PK，格式 `session前缀#序号` |
| session_id | 所属会话 |
| turn_start / turn_end | 覆盖的轮次范围 |
| headline | 一句话主题（全院可见） |
| share | 共享记忆正文 |
| private | 房间私有记忆正文 |
| timestamp | 原始对话发生时间 |
| room | room 名，如 main / secondary |
| model | 生成用的模型 |
| source | adapter/source namespace（如 `porch` / `phone` / `claude-code`）；不是 speaker、模型或 room。v14 起持久化，v15 将项目目录中的 `phone-*.jsonl` 从 Claude Code 正常会话中分离。 |

## card_nodes（tree Card node membership，v13）

`card_id -> node_id` 是 Card 与其实际覆盖的 tree message nodes 的多对多映射。
Card 的 `turn_start / turn_end` 是 inclusive；相邻滚动边界、fork child 重喂 parent tail
以及共享 ancestor context 都可能让同一个 canonical node 同时出现在多张 Card 中。
因此唯一键只有 `(card_id, node_id)`，没有全局 `UNIQUE(node_id)`。删除可塑尾卡时该卡的
membership 随 FK cascade 删除，成功新尾再按实际范围重建。

“这条 node 是否已经作为某个 branch 的新增物料处理过”不能用全局 node membership 判断；
tree trigger 按生成 Card 的 `session_id` 检查 branch-local coverage。共享 context 不单独触发
新卡，但可以合法地被新卡再次包含和重新组织。

## card_tags（标签，entity overlap + 共现的源）

| 字段 | 说明 |
|------|------|
| card_id | FK → cards |
| tag | 标签文本（小写） |

`PRIMARY KEY (card_id, tag)`

## entities / tag_entity_map（Leiden index 实体层）

`card_tags` 保留模型抽取的原始标签；Leiden index 通过 `tag_entity_map` 把标签解析到规范实体。

### entities

| 字段 | 说明 |
|------|------|
| entity_id | 自增 PK |
| canonical_name | 规范实体名，唯一 |
| name_embedding | 新 tag 在线去重用；历史 backfill 可为 NULL |
| created_at | 入库时间 |

### tag_entity_map

| 字段 | 说明 |
|------|------|
| tag | PK，原始标签 |
| entity_id | FK → entities |

## card_raw（原始输出备份）

| 字段 | 说明 |
|------|------|
| card_id | PK FK → cards |
| raw | 模型原始输出 |
| label | 来源标签 |
| source_file | 来源文件 |
| created_at | 入库时间 |

## cards_fts（全文索引，standalone FTS5 + jieba 预分词）

| 字段 | 说明 |
|------|------|
| card_id | UNINDEXED，回连 cards |
| headline | jieba 分词后的 headline |
| share | jieba 分词后的 share |
| private | jieba 分词后的 private |

当前正式检索对全部可见卡搜索 `{headline share}`，并且只对 viewer 本房卡搜索
`private`；跨房间不会拿 private 正文参与匹配。tags 是独立的补充匹配路径。
`private` 仅在 `--card` 且 viewer 与 `cards.room` 相同时展示。Python 侧写入和查询都先过
jieba。Last24 热层只展示 `headline`。

## clusters（Leiden index 层次社区）

| 字段 | 说明 |
|------|------|
| cluster_id | PK，如 `c0001` |
| summary | 当前社区的 top 规范实体摘要 |
| parent_cluster_id | FK → clusters；顶层为 NULL |
| level | 递归深度 |

## cluster_members（簇成员，card↔cluster 多对多）

| 字段 | 说明 |
|------|------|
| card_id | FK → cards |
| cluster_id | FK → clusters；primary 只挂叶社区 |
| role | primary / secondary |

`PRIMARY KEY (card_id, cluster_id)`

## 中期层（馆员 / Midlayer，v7）

夜间 Leiden 重跑后由 `treesnap.py` / `curator.py` 读写。运维见 `skills/ops/midlayer.md`。`night` 一律为**本地日期字符串**（如 `2026-07-03`，按 settings.timezone）。

### tree_snapshots（夜间树快照）

每晚把当前 `clusters` 原样拷一份，供「今夜 X = 昨夜 Y」的叶子层身份匹配。

| 字段 | 说明 |
|------|------|
| night | 本地日期字符串 |
| cluster_id | 该夜的社区 id |
| parent_cluster_id | 顶层为 NULL |
| level | 递归深度 |
| summary | 快照当时的社区摘要 |

`PRIMARY KEY (night, cluster_id)`

### tree_snapshot_members（快照成员）

| 字段 | 说明 |
|------|------|
| night | 本地日期字符串 |
| cluster_id | 所属社区 |
| card_id | 卡 |
| role | primary / secondary（与 cluster_members 同构） |

`PRIMARY KEY (night, cluster_id, card_id)`

### digests（每房间每晚近况小结）

由 curator agent（`cli.py --curate`）从当前记忆树**每夜白纸重写**，上限取
`settings.midlayer.digest_max_tokens`（兼容旧键 `digest_max_chars`）。房间 `digest.md`
覆盖式；此表只留每晚历史并作为“上次成功 curator”的水位，不回喂下一晚 prompt。

| 字段 | 说明 |
|------|------|
| night / room | PK，本地日期 + 房间 |
| body | 小结正文（房间 `digest.md` 另存，此处留历史） |
| model | 生成用模型 |
| created_at | 入库时间 |

### constants（constant 篮子）

只说过一次但永久有效的事实（生日等）。由 curator agent（`cli.py --curate`）增量维护（add/update/retire），重渲染到 `room_dir/constants.md`。`shared` 与卡层 share/private 同构：`shared=1` 全院可见，否则仅 `room` 可见。

| 字段 | 说明 |
|------|------|
| constant_id | PK |
| room | 归属房间 |
| shared | 0/1 |
| content | 事实正文 |
| source_card_id | 出处卡 |
| status | `active` / `retired` |
| first_seen / updated_at | 时间戳 |

## pipeline_runs / model_calls（审计记录）

运行日志，只追加不删改。`model_calls.step` 分两层：`gen_cards_attempt*`、`entity_resolve_attempt*` 与 `curate:<room>:attempt` 是物理模型尝试（prompt/raw/退出状态逐次留档）；`gen_cards` / `gen_cards_update` 与 `curate:<room>` 是业务逻辑成功；`curate:<room>:error` 是无合法提交。统计模型实际调用次数看 `*attempt*`，不要数逻辑成功行。

## session_forks（会话 fork 关系，v6）

Claude Code 撤回/重发或手动 fork 时会新建 session JSONL，但把旧 transcript 原样复制进去（复制行保留原 `source_uuid`）。此表记录推断出的 child→parent 关系和 fork 点，让首次 eligibility 按 delta 部分计算，同时允许 Card Gen 把 parent tail 作为重叠 context。由 `refresh_session_forks`（db.py）重建。

| 字段 | 说明 |
|------|------|
| child_session_id | PK，fork 出来的子会话 |
| parent_session_id | 被复制的父会话 |
| fork_round | 子会话里的分叉轮次 |
| parent_fork_round | 父会话里对应的分叉轮次 |
| delta_start_round | fork 后第一轮新增内容；用于首次 eligibility 与无可复用 parent tail 时的 context fallback，不保证等于实际 Card Gen 重喂起点 |
| shared_turns | 与父会话共享的 turn 数 |
| child_turns / parent_turns | 子 / 父会话各自的 turn 数 |
| child_shared_ratio | 共享占子会话的比例（推断置信度） |
| detected_at | 入库时间 |

`session_forks_parent_idx` 索引 `parent_session_id`。

## card_access（卡片取用日志，v8）

重量不在写入时标，用「被重新拿起」度量。`retrieval.py` CLI 的 `--card` 展开时记一笔（列表扫过不算）；`RECALL_NO_LOG=1` 跳过写入，批量脚本防污染用。curator 工作台的 recall 指向自己的 view.db 快照，天然进不了主库。读端在 `treesnap.render_report`：聚合成顶层社区 `↻取用` 计数和「本期被重新取用的卡」清单，随树报告喂 curator——写入冷但取用热的线也算有动静。

| 字段 | 说明 |
|------|------|
| card_id | 被展开的卡 |
| viewer | 谁在翻（room slug，如 `main` / `secondary`） |
| source | 来路，聚合只认 `search` |
| ts | UTC ISO（写入方显式给，与 cards.timestamp 可比；DEFAULT 兜底） |

`card_access_ts_idx` 索引 `ts`；`card_access_card_idx` 索引 `(card_id, ts)`。
