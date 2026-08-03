# 运维 · Common（跨模块 / 运行环境）

> launchd 服务、迁移、备份、整体文件布局。模块专属命令见各自的 ops 文件。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## launchd 定时任务

| Label | 时间 | 干什么 |
|------|------|------|
| `local.recall-watch-ingest` | 实时（KeepAlive） | fswatch Claude Code JSONL → legacy Card turns + canonical tree/history；不调用模型 |
| `local.recall-watch-cards` | 白天实时（KeepAlive） | 轮询 DB `turns` 水位 → `--auto-cards`；所有 ingest 来源共用 |
| `local.recall-nightly` | 04:00 | 索引 + cards-last-24 + 可选 last24 summary + curator；`nightly.generate_missing_cards=true` 时才补漏出卡 |
| `local.recall-backup` | 定时 | `bin/backup-db.sh`：DB .backup → 配置的备份目录 |

> v3 的 `local.recall-watch-process`（实时完整管线）已废弃，plist 归档于 `attic/recall-v3-watch-process/`。白天出卡由独立 DB card watcher 承担，nightly 只做冷 session 补漏及夜间派生层。
> 本文示例用 `local.recall-*` 是历史标签；新装部署由 `bin/make-launchd.sh` 生成，默认标签是 `com.neroli.*`——把下面命令里的标签换成你自己的。

```sh
launchctl list | grep recall                                              # 状态
tail -f data/watch.log data/card-watch.log                                 # 两个白天 watcher 日志
tail -f data/nightly.log                                                   # nightly 日志
launchctl kickstart -k gui/$(id -u)/local.recall-watch-ingest              # 重启 ingest watcher
launchctl kickstart -k gui/$(id -u)/local.recall-watch-cards               # 重启 DB card watcher

# 停 / 起（改 src/ 前必停，否则 watcher 会拿改到一半的代码跑真库）
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootout gui/$(id -u)/local.recall-watch-cards
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-cards.plist
```

## 运行时依赖

| 依赖 | 用途 | 安装 | 检查 |
|------|------|------|------|
| `fswatch` | watcher 文件监听 | `brew install fswatch` | `which fswatch` |
| `codex` | 模型调用（出卡 / 实体去重 / curator） | ChatGPT.app 内置，或 `npm install -g @openai/codex` | `which codex` |

`codex` 丢失时的表现：ingest watcher 和 cards rebuild 正常运行（无模型调用），但 card watcher 的 `auto-cards`、last24 summary、实体去重、nightly curator 失败（日志报 `No such file or directory: 'codex'`）。`cards-last-24.md` 照常刷新，**只是不再产出新卡/新 summary**——容易误判为"一切正常"。

`settings.model_access.provider=cli` 且 `cli_cmd` 为空时默认走 codex；填了 `cli_cmd`（如 `claude -p`）则用自定义命令，不依赖 codex。查找顺序：`$CLAUDE_MEMORY_CODEX_BIN` → PATH → ChatGPT.app/Codex.app 内置路径 → 裸 `codex`（见 `src/model.py:_codex_bin`）。

默认 codex 命令会通过 `model_instructions_file` 指向仓库内的
`prompts/minimal-instructions.txt`（运行时解析成绝对路径），以 `.` 替换 Codex 内置的
base instructions；卡片、实体裁判和 curator 各自的任务 prompt 仍照常从 stdin 传入。

App 更新可能改变内置路径——出卡突然全部静默失败时优先查这个。

## 运行锁

两个白天 watcher 与 nightly 共用 `data/ingest.lock.d`，实现集中在 `bin/lock-lib.sh`。锁目录里的
`owner` 记录实际子进程 PID 和本轮 token：PID 仍活着就永不按运行时长抢锁（curator 单次
timeout 可达 900 秒，不能再用旧的 300 秒年龄阈值）；owner 已死才回收。释放时必须 token
匹配，旧任务不能删除后来任务的锁。普通调用默认最多等约 60 秒，超时日志会带 owner PID
与持锁时长；nightly 设置为持续排队，避免白天出卡先拿锁、或 Mac 中途睡眠
拉长任务墙钟时间时，整班夜间维护因 60 秒竞争超时而取消。

手工清锁前先读 `data/ingest.lock.d/owner` 并用 `kill -0 PID` 确认进程确实不存在；活进程
禁止直接删锁，否则可能让两个出卡/curator 同时写库。

## 调参（config/settings.json → `watcher` / `nightly`）

改完即生效，无需重启 launchd。

| 参数 | 默认 | 说明 |
|------|------|------|
| `watcher.ingest_debounce_seconds` | 5 | fswatch 全量 ingest 的去抖间隔 |
| `watcher.auto_cards` | true | DB `turns` 水位变化时，白天 card watcher 是否调用 `--auto-cards` |
| `watcher.card_poll_seconds` | 5 | card watcher 轮询 DB 水位的间隔 |
| `ingest.tree_card_projection_sources` | 所有 tree source | 迁移开关；只可暂时排除仍由同一 journal 的 legacy loader 唯一出卡的来源 |
| `nightly.generate_missing_cards` | false | 夜间是否给无卡 session 补漏出卡（避免大额模型重跑） |

## 备份

```sh
bin/backup-db.sh                       # DB .backup（自动处理 WAL）→ 配置的备份目录
python3 scripts/backup_cards.py        # 卡层 JSON 快照 → data/backups/
```

## schema 迁移

规则：版本变更走 `migrations/NNN-*.sql`（手动 `sqlite3 db < 迁移文件`）+ `PRAGMA user_version` 递增，**禁止运行时迁移**。`connect()` 只校验版本。表结构见 [schema.md](../../schema.md)。v10 的 `change_watermarks.turns` 是所有 ingest 来源共用的白天 Card Gen 唤醒边界；v11 增加 canonical adapter provenance 与本机 room policy 结果；v12 增加 conversation tree 与渲染 observation；v13 把 `card_nodes` 校正为允许 inclusive/fork overlap 的多对多 membership。

迁移期间**必须停两个白天 watcher**：既防止 ingest 边迁移边写库，也防止旧 card watcher
拿新库运行。nightly 若正运行也必须等它正常结束；不要抢活锁。

<details>
<summary>v12→v13 overlapping Card node membership 迁移</summary>

```sh
# 1. 停 ingest/card watcher，确认 nightly 和公用锁都不活跃
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootout gui/$(id -u)/local.recall-watch-cards 2>/dev/null || true
test ! -d data/ingest.lock.d

# 2. 在公开仓库外做 SQLite 在线备份
sqlite3 data/fragments.db ".backup '/绝对路径/fragments-before-v13.db'"

# 3. 显式迁移；保留既有 membership，移除 node_id 全局 UNIQUE
sqlite3 data/fragments.db < migrations/013-card-node-membership.sql

# 4. 验证版本、完整性与 legacy/Card 计数
sqlite3 data/fragments.db "PRAGMA user_version; PRAGMA quick_check;
  PRAGMA foreign_key_check;
  SELECT COUNT(*) FROM messages;
  SELECT COUNT(*) FROM turns;
  SELECT COUNT(*) FROM cards;
  SELECT COUNT(*) FROM card_nodes;"

# 5. 重启两个白天 watcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-cards.plist
```

</details>

<details>
<summary>v11→v12 conversation tree + observation 迁移</summary>

```sh
# 1. 停 ingest/card watcher，确认 nightly 和公用锁都不活跃
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootout gui/$(id -u)/local.recall-watch-cards 2>/dev/null || true
test ! -d data/ingest.lock.d

# 2. 在公开仓库外做 SQLite 在线备份
sqlite3 data/fragments.db ".backup '/绝对路径/fragments-before-v12.db'"

# 3. 显式迁移（不重写旧 messages/turns/cards）
sqlite3 data/fragments.db < migrations/012-conversation-tree.sql

# 4. 验证版本、旧层计数、新表与完整性
sqlite3 data/fragments.db "PRAGMA user_version; PRAGMA quick_check;
  PRAGMA foreign_key_check;
  SELECT COUNT(*) FROM messages;
  SELECT COUNT(*) FROM turns;
  SELECT COUNT(*) FROM cards;
  SELECT COUNT(*) FROM conversation_nodes;
  SELECT COUNT(*) FROM conversation_observations;"

# 5. 配好 ingest.source_rooms 后重启；旧 source_routes 目标 room 可作过渡 allowlist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-cards.plist
```

</details>

<details>
<summary>v10→v11 canonical adapter contract 迁移</summary>

```sh
# 1. 停两个 watcher，确认 nightly/锁不在运行
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootout gui/$(id -u)/local.recall-watch-cards 2>/dev/null || true
test ! -d data/ingest.lock.d

# 2. 在线备份；目标必须在公开仓库外
sqlite3 data/fragments.db ".backup '/绝对路径/fragments-before-v11.db'"

# 3. 显式迁移（旧数据不伪造 adapter provenance）
sqlite3 data/fragments.db < migrations/011-canonical-adapter-contract.sql

# 4. 验证旧层计数、v11 表/列和完整性
sqlite3 data/fragments.db "PRAGMA user_version; PRAGMA quick_check;
  PRAGMA foreign_key_check;
  SELECT COUNT(*) FROM source_sessions;
  SELECT COUNT(*) FROM messages;
  SELECT COUNT(*) FROM turns;"

# 5. 为每个 v2 adapter 配好私有 ingest.source_routes 后再起 watcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-cards.plist
```

</details>

<details>
<summary>v9→v10 turns 水位迁移</summary>

```sh
# 1. 停两个白天 watcher，并确认 nightly / 锁未运行
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootout gui/$(id -u)/local.recall-watch-cards 2>/dev/null || true
test ! -d data/ingest.lock.d

# 2. 用 SQLite 在线备份做可恢复快照
sqlite3 data/fragments.db ".backup '/绝对路径/fragments-before-v10.db'"

# 3. 显式迁移（不调用模型）
sqlite3 data/fragments.db < migrations/010-turns-watermark.sql

# 4. 验证版本、完整性、水位
sqlite3 data/fragments.db "PRAGMA user_version; PRAGMA quick_check;
  PRAGMA foreign_key_check;
  SELECT name, revision, updated_at FROM change_watermarks;"

# 5. 起两个白天 watcher；首次启动只建立当前水位基线，不补跑旧事件
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-cards.plist
```

</details>

<details>
<summary>v3→v4 Option A runbook（已执行，留作复现 / 回滚参考）</summary>

Option A = 保留旧 v3 卡，只重建 turns/messages 原始层。

```sh
# 1. 停 watcher
launchctl bootout gui/$(id -u)/local.recall-watch-ingest

# 2. 备份：卡层 JSON + 全库快照
python3 scripts/backup_cards.py
sqlite3 data/fragments.db ".backup data/backups/fragments-v3-before-v4.db"

# 3. 迁移（migrations/004 里 DELETE 卡的块默认注释）
sqlite3 data/fragments.db < migrations/004-turns-v4.sql

# 4. 只 ingest 不出卡
bin/cli.py --ingest-only --max-messages 0
python3 scripts/ingest_claude_ai_exports.py

# 5. 验证：user_version / messages,turns 有数据 / 孤儿 turns=0
sqlite3 data/fragments.db "PRAGMA user_version;
  SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM turns), (SELECT COUNT(*) FROM cards);
  SELECT COUNT(*) FROM turns t LEFT JOIN messages m ON m.source_uuid=t.source_uuid WHERE m.source_uuid IS NULL;"

# 6. 起 watcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist

# 回滚：cp data/backups/fragments-v3-before-v4.db data/fragments.db（代码同步回退）
```
</details>

## 文件布局

```
recall-pipeline/
├── config/
│   ├── rooms.json          # 房间唯一事实源
│   ├── settings.json       # 可调参数
│   └── schema.sql          # v13 建表 DDL（只用于新库）
├── prompts/
│   ├── agent-persona-<room>.md
│   └── gen-cards-prompt.md
├── src/                    # 模块归属见 ARCHITECTURE.md「模块地图」
├── bin/
│   ├── cli.py              # 外层 wrapper（加 src/ 到 sys.path）
│   ├── watch.sh            # fswatch Claude Code → ingest
│   ├── watch-cards.sh      # 轮询 DB turns 水位 → 白天 Card Gen
│   ├── nightly.sh          # 凌晨 4 点：index + cards + last24 summary + curator
│   └── backup-db.sh        # DB 在线备份
├── skills/
│   ├── install/            # 安装 Skill
│   └── ops/                # 按模块拆的运维 Skill（本目录）
│       └── SKILL.md        # 运维 Skill 入口 + 索引
├── data/
│   ├── fragments.db        # 生产库（schema v13）
│   ├── backups/ .emb_cache/ watch.log nightly.log
├── migrations/             # 002…013（已有库的显式 schema 迁移）
├── tests/                  # 按组件归属分组的公开回归测试
├── ARCHITECTURE.md         # 模块地图（总枢纽）
└── schema.md               # 表结构速查
```

recent cards 输出到 `config/rooms.json` 里每个房间的 `room_dir/cards-last-24.md`；
配置的 summary agent 所写核实版短小结输出到 `room_dir/summary-last-24.md`。
