# 运维 · Ingest（原文入库）

> 模块文件：`src/loaders.py`（+ `src/memory_types.py`）。读外部对话文件，写 `messages`/`turns`。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 只写入 turns，不跑模型
bin/cli.py --ingest-only input.jsonl

# 全量重读（round 跨文件一次算；--max-messages 0 = 不截断）
bin/cli.py --ingest-only --max-messages 0

# 只导入 Claude.ai 下载包里的 conversations.json（不碰 projects/users/memories）
python3 scripts/ingest_claude_ai_exports.py --dry-run
python3 scripts/ingest_claude_ai_exports.py
```

Claude Code 的实时 ingest 由 launchd fswatch watcher 自动跑（见 [common.md](common.md)）。
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
- **Claude.ai 导出** — `backups/origin-data/**/conversations.json`，一次性手动 backfill；写入 turns 后同样抬 DB 水位
- **normalized adapter v2** — `neroli-normalized-v2` envelope；adapter 交 native identity、
  不可变原文、UTC 发生时间、稳定 `source_sequence` 与 `source_route`，Neroli 统一生成
  canonical session/message ID、round 和 room。完整 contract 见
  [docs/normalized-adapter-contract.md](../../docs/normalized-adapter-contract.md)。
- **legacy normalized / test** — 旧 JSON 数组与 `*.txt` 继续兼容，但新 adapter 不再使用。

> 接其他对话源：只产出 v2 envelope，不在 adapter 里耦合 Card Gen，也不让 adapter 直接
> 指定 room。adapter 保留富原生 session；Neroli 只消费公开 contract，本机私有
> `ingest.source_routes` 决定 `(source, source_route) → room`。未知 route 必须先补 policy。

## 新来源 / 清洗脚本

不同平台的导出结构、撤回/编辑语义、附件占位、时间戳和 fork 规则都可能不同。不要把新来源硬塞进现有 Claude.ai 脚本；先写一个小的 normalize/clean 脚本，把原始导出转成 loader 能稳定消费的记录，再接 `src/loaders.py`。

验收新来源时至少检查：

- dry-run 打印样本数、时间范围、会话数，不写库。
- `native_message_id` 在一个 source 内全局稳定；若平台 ID 只在 session 内唯一，adapter 先加 session 前缀。
- `native_session_id` 表达真实会话边界；`source_sequence` 在 session 内稳定且唯一。
- 重复导入不产生新 turn；同 native message ID 改内容会硬失败，真实编辑必须用新 ID。
- `source_route` 在私有 policy 中有且只有一个 room 映射；删掉映射的测试必须拒绝入库。
- 清洗只去掉平台噪声（空消息、附件壳、系统占位等），不改写用户/agent 原话。
- 导入后抽查 `messages` / `turns` 计数、首尾时间、几条原文内容；再跑一次 ingest，计数不应重复增长。
