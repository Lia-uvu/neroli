# 运维 · Ingest（原文入库）

> 模块文件：`src/loaders.py`（+ `src/memory_types.py`）。读外部对话文件，写 `messages`/`turns`。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 只写入 turns，不跑模型
bin/cli.py --ingest-only input.jsonl

# 跨 room/source 分页列最近 context（不写库；limit 1–100，默认 30）
bin/cli.py --list-history-contexts --history-limit 30 --history-offset 0

# 诊断时可用 room/source 缩窄同一列表
bin/cli.py --list-history-contexts --history-room ROOM --history-source porch \
  --history-limit 30 --history-offset 0

# 只读选中 context 所在的完整 component + sibling branches + 最新 cursor path
bin/cli.py --render-history --history-room ROOM --history-source porch \
  --history-context NATIVE_CONTEXT_ID

# 不带 --history-context 会返回整间 room，只保留给人工诊断，不是 GUI 入口
bin/cli.py --render-history --history-room ROOM --history-source porch

# 全量重读（round 跨文件一次算；--max-messages 0 = 不截断）
bin/cli.py --ingest-only --max-messages 0

# Claude Code canonical tree 兼容性审计：隔离临时库运行两遍，不改生产、不跑模型
python3 scripts/audit_claude_tree_adapter.py

# 只把 Claude Code tree/observations 幂等回填到指定库；默认不生成 Card turns、不跑模型
python3 scripts/ingest_claude_tree.py --dry-run
python3 scripts/ingest_claude_tree.py --db data/fragments.db

# 只修复旧 tree adapter 曾误标为 message 的 Claude meta/API-error 节点；
# 默认先 dry-run，确认候选没有任何 Card projection 后再显式应用并幂等回填
python3 scripts/ingest_claude_tree.py --db data/fragments.db \
  --repair-portable-noise --dry-run
python3 scripts/ingest_claude_tree.py --db data/fragments.db \
  --repair-portable-noise

# 只修复旧 adapter 直接照搬 runtime retry/compaction parent 所留下的错误边；
# 同样先 dry-run，且只允许 history-only、零 Card projection 的 Claude tree
python3 scripts/ingest_claude_tree.py --db data/fragments.db \
  --repair-history-structure --dry-run
python3 scripts/ingest_claude_tree.py --db data/fragments.db \
  --repair-history-structure

# 清理电话亭明确标记为未送达的 user 尝试；默认 dry-run。
# apply 前停 ingest/card watcher、确认无活锁并在线备份数据库；apply 后全量 ingest 重编号，
# 再运行 --rebuild-cards 重建零模型派生视图。覆盖失败轮次的旧卡会被删除，不自动付费重喂。
python3 scripts/repair_phone_failed_attempts.py
python3 scripts/repair_phone_failed_attempts.py --apply

# 只导入 Claude.ai 下载包里的 conversations.json（不碰 projects/users/memories）
python3 scripts/ingest_claude_ai_exports.py --dry-run
python3 scripts/ingest_claude_ai_exports.py

# 将同一 archive 回填到 canonical History；默认只报结构计数，显式 --apply 才写库。
# 先在私有 settings.ingest.source_rooms 授权 claude-ai -> ROOM；这条路径固定 history-only，
# 不写第二份 turns/Cards，不跑模型。生产 apply 前仍须停 watcher、备份并检查活锁。
python3 scripts/backfill_claude_ai_history.py --room ROOM
python3 scripts/backfill_claude_ai_history.py --room ROOM --apply
```

Claude Code 的实时 ingest 由 launchd fswatch watcher 自动跑（见 [common.md](common.md)）。
watcher 同一轮读取原始 JSONL：既有 loader 继续更新旧 Card turns，Claude tree adapter
同时幂等更新 canonical tree/history。当前本机 `tree_card_projection_sources` 不含
`claude-code`，所以 tree 侧不会再投影第二份 turns；移除旧 loader 前不得启用该项。
其他来源可以有各自的 adapter / 调度；所有来源最终只写统一的 `source_sessions` / `messages` / `turns`。schema v10+
在 `turns` 上维护来源无关的变更水位，独立白天 card watcher 因而无需知道是哪一个 adapter 写入。

## 增量 ingest（`settings.watcher.incremental_ingest`，默认开）

watcher 的全房间重读默认走增量：只重读 mtime 变过的文件，**外加与它们共享 session_id 的
文件**——因为 fork/续接会把一个 session 的行劈到多个文件（重放行保留原 sessionId），必须让
每个被触碰的 session 从它的全部文件一起重编号，否则 `assign_rounds` 会把局部重编号写坏库。
实现取 (文件 ↔ session) 二部图里含变化文件的连通分量（`src/incremental.py`）。

- 状态：`data/ingest-state.json`（{文件: mtime}），ingest 成功后才推进。
- **强制全量重读**：删掉 `data/ingest-state.json`，或把 `incremental_ingest` 设 false。
- 只作用于扫描模式（不给输入/session 的全房间重读）；显式 `cli.py x.jsonl` 手动调用照旧全读。
- fork/撤回零损失：编辑只落最近文件（必被重读），冷文件已在库，fork 检测只读库不读文件。

## 来源家族

加载按来源家族路由（`load_messages_for_ingest`），三族的 `source_uuid` / `session_id` / 排序规则见 [schema.md](../../schema.md) 「三个 source 家族」：

- **Claude Code JSONL** — `~/.claude/projects/<room>/*.jsonl`，watcher 实时 tail 已配置的 room
- **Claude.ai 导出** — `backups/origin-data/**/conversations.json`，一次性手动 backfill；既有
  loader 写入 turns 后同样抬 DB 水位。另有默认 dry-run 的 canonical History backfill，沿显式
  parent 建树并用 archive-root 聚合同一 conversation；多个 full export 按 conversation
  `updated_at` 选择最新完整快照，缺旧节点或同时间冲突则停止。该 bridge 固定 history-only，
  不重复写 turns/Cards
- **normalized adapter v2** — `neroli-normalized-v2` envelope；adapter 交 native identity、
  不可变原文、UTC 发生时间、稳定 `source_sequence` 与 `source_route`，Neroli 统一生成
  canonical session/message ID、round 和 room。完整 contract 见
  [docs/normalized-adapter-contract.md](../../docs/normalized-adapter-contract.md)；adapter / Neroli
  职责、当前 tree 边界与未实现语义见
  [docs/source-adapter-boundary.md](../../docs/source-adapter-boundary.md)。
  这是线性 adapter 的兼容格式。
- **conversation tree v1** — `neroli-conversation-tree-v1` envelope；adapter 交完整
  `node + parent` 原文树、明确服务的 `room` 和可选 observation。Neroli 用
  `ingest.source_rooms` 校验来源是否可写该 room；若尚未配新键，会以该来源
  现有 `source_routes` 的目标 room 集作过渡 allowlist。节点可增量交付，缺席不是
  删除。完整 contract 见
  [docs/conversation-tree-adapter-contract.md](../../docs/conversation-tree-adapter-contract.md)。
- **legacy normalized / test** — 旧 JSON 数组与 `*.txt` 继续兼容，但新 adapter 不再使用。

> 接无原生分叉的线性来源：可继续产出 v2 envelope，不在 adapter 里耦合 Card Gen，也不让 v2 adapter 直接
> 指定 room。adapter 保留富原生 session；Neroli 只消费公开 contract，本机私有
> `ingest.source_routes` 决定 `(source, source_route) → room`。未知 route 必须先补 policy。

> 新的树形来源优先产出 tree v1，不要先摊平成 v2 session。adapter 只翻译
> 原文树与它能权威观测的 runtime cursor；不提交 main/active/abandoned 节点属性。

当前 v2 更新一个已有 native session 时，要重放该 session 已知的完整有序轨迹；不要只交
新增 delta。round/message_seq 是 loader 根据本次交付内的轨迹推导的，而缺席的旧节点也不会
被当成删除。tombstone 和 branch replacement 尚不在公开 contract 内。

Tree v1 的 observation 只写 `conversation_observations`。验收时记录入库前后
`change_watermarks.turns`：只改 cursor/observation 时 revision 必须不变；`--render-history`
的 observed path 应更新。

GUI/history reader 应先调用 bounded context list；统一列表可以不传 room，但每项必须保留返回的
room/source/context，再把三者明确传给 selected render。选中读取必须包含同一 root 下 sibling
branches，但不得带入其他 root；
contract test 用两个独立 context 检查分页、`has_more` 与 component 隔离。不要把不带 context 的
full-room diagnostic render 接成产品列表。

迁移期的 history-only ingest 还必须同时核对 `model_calls`、legacy message/turn/Card 计数和
watermark 均不变。`scripts/audit_claude_tree_adapter.py` 会在隔离库两次导入全部已配置
Claude corpus，并双向对照旧 loader 的 portable message identity/role/text；tree 多出或漏掉
message 都会失败；同时逐条核对 API retry 与 compact boundary 的 source-authoritative parent
normalization。它不输出正文，也不调用模型。
生产回填前先停两个 watcher 并备份数据库，回填后重复运行一次确认幂等，再按 common.md 起回。

## 新来源 / 清洗脚本

不同平台的导出结构、撤回/编辑语义、附件占位、时间戳和 fork 规则都可能不同。不要把新来源硬塞进现有 Claude.ai 脚本；先写一个小的 normalize/clean 脚本，把原始导出转成 loader 能稳定消费的记录，再接 `src/loaders.py`。

验收新来源时至少检查：

- dry-run 打印样本数、时间范围、会话数，不写库。
- `native_message_id` 在一个 source 内全局稳定；若平台 ID 只在 session 内唯一，adapter 先加 session 前缀。
- `native_session_id` 表达真实会话边界；`source_sequence` 在 session 内稳定且唯一。
- 重复导入不产生新 turn；同 native message ID 改内容会硬失败，真实编辑必须用新 ID。
- `source_route` 在私有 policy 中有且只有一个 room 映射；删掉映射的测试必须拒绝入库。
- tree v1 的 `room` 必须在 `ingest.source_rooms[source]` 中；整树应入
  `conversation_nodes`。分叉的新增物料只归一个 processing stream 触发，但 inclusive/fork
  Card 边界允许同一节点出现在多张 `card_nodes` membership 中。
- 清洗只去掉平台噪声（空消息、附件壳、系统占位等），不改写用户/agent 原话。
- phone journal 的 `phone-error.parentUuid` 明确表示对应 user 未成功送达；legacy turns 跳过该
  user，canonical tree 保留结构 event 但不投影 portable message。重试成功会以新的原生 UUID
  形成正常 user/assistant，不靠正文相似度判断。
- 导入后抽查 `messages` / `turns` 计数、首尾时间、几条原文内容；再跑一次 ingest，计数不应重复增长。
