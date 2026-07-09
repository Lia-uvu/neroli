# 运维 · Common（跨模块 / 运行环境）

> launchd 服务、迁移、备份、整体文件布局。模块专属命令见各自的 ops 文件。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## launchd 定时任务

| Label | 时间 | 干什么 |
|------|------|------|
| `local.recall-watch-ingest` | 实时（KeepAlive） | fswatch → ingest；`watcher.auto_cards=true` 时顺带自动出卡 |
| `local.recall-nightly` | 04:00 | 默认只聚类 + context rebuild；`nightly.generate_missing_cards=true` 时才补漏出卡 |
| `local.recall-backup` | 定时 | `bin/backup-db.sh`：DB .backup → 配置的备份目录 |

> v3 的 `local.recall-watch-process`（实时完整管线）已废弃，plist 归档于 `attic/recall-v3-watch-process/`。出卡改由 `watcher.auto_cards` + nightly 补漏承担。
> 本文示例用 `local.recall-*` 是历史标签；新装部署由 `bin/make-launchd.sh` 生成，默认标签是 `com.neroli.*`——把下面命令里的标签换成你自己的。

```sh
launchctl list | grep recall                                              # 状态
tail -f data/watch.log                                                     # watcher 日志
tail -f data/nightly.log                                                   # nightly 日志
launchctl kickstart -k gui/$(id -u)/local.recall-watch-ingest             # 重启 watcher

# 停 / 起（改 src/ 前必停，否则 watcher 会拿改到一半的代码跑真库）
launchctl bootout gui/$(id -u)/local.recall-watch-ingest
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.recall-watch-ingest.plist
```

## 调参（config/settings.json → `watcher` / `nightly`）

改完即生效，无需重启 launchd。

| 参数 | 默认 | 说明 |
|------|------|------|
| `watcher.ingest_debounce_seconds` | 5 | fswatch 全量 ingest 的去抖间隔 |
| `watcher.auto_cards` | true | watcher ingest 后是否顺带自动出卡 |
| `nightly.generate_missing_cards` | false | 夜间是否给无卡 session 补漏出卡（避免大额模型重跑） |

## 备份

```sh
bin/backup-db.sh                       # DB .backup（自动处理 WAL）→ 配置的备份目录
python3 scripts/backup_cards.py        # 卡层 JSON 快照 → data/backups/
```

## schema 迁移

规则：版本变更走 `migrations/NNN-*.sql`（手动 `sqlite3 db < 迁移文件`）+ `PRAGMA user_version` 递增，**禁止运行时迁移**。`connect()` 只校验版本。表结构见 [schema.md](../../schema.md)。

迁移期间**必须停 watcher**：既防止它边迁移边写库，也防止迁移后旧进程拿新库乱跑污染数据。

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
│   └── schema.sql          # v7 建表 DDL（只用于新库）
├── prompts/
│   ├── agent-persona-<room>.md
│   └── gen-cards-prompt.md
├── src/                    # 模块归属见 ARCHITECTURE.md「模块地图」
├── bin/
│   ├── cli.py              # 外层 wrapper（加 src/ 到 sys.path）
│   ├── watch.sh            # fswatch 实时监听
│   ├── nightly.sh          # 凌晨 4 点：Leiden index + context rebuild
│   └── backup-db.sh        # DB 在线备份
├── skills/
│   ├── install/            # 安装 Skill
│   └── ops/                # 按模块拆的运维 Skill（本目录）
│       └── SKILL.md        # 运维 Skill 入口 + 索引
├── data/
│   ├── fragments.db        # 生产库（schema v7）
│   ├── backups/ .emb_cache/ watch.log nightly.log
├── migrations/             # 002…007-midlayer.sql
├── tests/                  # 本地回归测试；公开库不带
├── ARCHITECTURE.md         # 模块地图（总枢纽）
└── schema.md               # 表结构速查
```

context 输出到 `config/rooms.json` 里每个房间的 `room_dir/context-last-24.md`。
