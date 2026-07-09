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

实时 ingest 由 launchd watcher 自动跑（见 [common.md](common.md)）。

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
- **Claude.ai 导出** — `backups/origin-data/**/conversations.json`，一次性手动 backfill
- **normalized / test** — `*.json` / `*.txt`，确定性哈希 id，dev/test 用

> 接其他对话源（Codex / Open Claw 等）：只要产出 messages/turns 需要的字段即可，给你的 agent 看 schema.md 让它写一个适配脚本。

## 新来源 / 清洗脚本

不同平台的导出结构、撤回/编辑语义、附件占位、时间戳和 fork 规则都可能不同。不要把新来源硬塞进现有 Claude.ai 脚本；先写一个小的 normalize/clean 脚本，把原始导出转成 loader 能稳定消费的记录，再接 `src/loaders.py`。

验收新来源时至少检查：

- dry-run 打印样本数、时间范围、会话数，不写库。
- `source_uuid` 稳定且全局唯一；同一原文重复导入不会产生新 turn。
- `session_id` 能表达真实会话边界；同一 session 内按时间和原数组下标稳定排序。
- 清洗只去掉平台噪声（空消息、附件壳、系统占位等），不改写用户/agent 原话。
- 导入后抽查 `messages` / `turns` 计数、首尾时间、几条原文内容；再跑一次 ingest，计数不应重复增长。
