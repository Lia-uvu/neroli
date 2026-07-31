# 运维 · Card Gen（出卡）

> 模块文件：`src/gen_cards.py`（+ `src/model.py`）。读 `turns`/`messages`，写 `cards`/`card_tags`/`card_raw`/`cards_fts`。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 全量出卡（已有卡的 session 跳过）
bin/cli.py --process-existing --skip-processed

# 双阈值自动出卡（按 settings.card_gen 阈值判断是否触发）
bin/cli.py --auto-cards

# 处理指定 session
bin/cli.py --process-existing --session-id SESSION_ID

# dry run：只看会跑哪些 session，不调模型
bin/cli.py --process-existing --skip-processed --dry-run

# 双阈值 dry run：看 --auto-cards 会不会触发、会选哪些 session，不调模型
bin/cli.py --auto-cards --dry-run
```

## 调参（config/settings.json → `card_gen`）

改完即生效，无需重启 launchd。

| 参数 | 默认 | 说明 |
|------|------|------|
| `min_new_turns` | 5 | 触发出卡的全局新 turns 最低门槛 |
| `min_interval_minutes` | 60 | 距上次出卡的最小间隔（分钟） |
| `min_first_session_turns` | 3 | 无卡新 session 首次出卡的最低 turns 数（≤2 turns 不出卡）；已有卡的 session 不受此限制 |
| `min_first_session_turns_exempt_source_globs` | `[]` | 来源文件匹配任一 SQLite GLOB 时，绕过首次出卡 turns 门槛；例如 `*/phone-*.jsonl` |
| `max_sessions_per_trigger` | 10 | 每次触发最多处理几个 session |
| `model` | gpt-5.5 | 模型名 |
| `reasoning_effort` | low | codex exec 的 reasoning effort。合法值：`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra`——注意 ChatGPT 界面的 "light thinking" 对应这里的 `low`，写 `light` 会被 codex 拒掉导致出卡静默失败 |
| `turn_cap` | 1600 | 单条 turn 最大字符数（截断） |
| `init_rounds` | 14 | 首次窗口大小（轮） |
| `grow_rounds` | 14 | 后续每次追加的轮数 |

## 白天实时唤醒（与 nightly 分开）

`bin/watch-cards.sh` 不监听任何单一来源文件；它轮询 schema v10 的
`change_watermarks(name='turns')`。SQLite trigger 只在 `turns` 业务字段实际增删改时递增
revision，所以 cards/index 写同一个 DB 不会自触发，幂等重扫也不会虚假唤醒。

水位变化后 watcher 调用现成的 `--auto-cards`，仍受上表的 5 rounds、60 分钟和每次最多
10 sessions 等闸门约束。一次水位变化只触发一次检查；与旧 fswatch 行为一致，冷却时间到期
本身不会制造新事件，下一次 `turns` 变化或 nightly 才会再检查。

开关和轮询间隔在 `settings.watcher`：

| 参数 | 默认 | 说明 |
|------|------|------|
| `auto_cards` | true | 是否在 DB turns 水位变化时调用模型出卡 |
| `card_poll_seconds` | 5 | 水位轮询间隔 |

nightly 仍独立负责冷 session 补漏、index、last24 与 curator；不要把白天 watcher 合并进
nightly，也不要让新 adapter 直接调用 Card Gen。adapter 只需正确写 `messages` / `turns`。

## 模型配置

默认模型：GPT-5.5 via Codex CLI，`reasoning_effort=low`。

`--auto-cards` 从 settings.json 读 `model` 和 `reasoning_effort`，自动构建 codex 命令。手动跑可覆盖：

```sh
bin/cli.py --process-existing --skip-processed --provider cli --model gpt-5.5
```

三后端（cli / ollama / api）实现见 `src/model.py`。

## 失败安全与审计

- 每个滚动窗口的 prompt、原始 stdout、解析结果都先以
  `model_calls.step=gen_cards_attempt` 留档；CLI 内部退避产生的每次物理 subprocess 也各占
  一行。解析/调用失败用 `gen_cards_attempt_error`，即使后窗失败，前窗已经付费的输出也不会
  随业务事务 rollback 消失。
- 任一窗口解析不到卡是**硬失败**：不删除旧尾卡、不写 `0 cards` 成功记录，CLI 返回非零。
  只有整组新卡合法生成后才替换旧尾卡并写逻辑完成行 `gen_cards` / `gen_cards_update`。
- 自动出卡的间隔以“最近一次尝试”计算，所以失败后也受 `min_interval_minutes` 冷却；待处理
  turns 仍以上次逻辑成功为界，冷却结束后可以重试，不会因失败审计而永久跳过。
- `--auto-cards` 有任一 session 失败就返回非零；成功的其他 session 仍会落盘并刷新派生层。
  查实际调用次数时数 `gen_cards_attempt*`，不要把逻辑完成行当物理调用数。

## prompt

出卡 prompt 模板在 `prompts/gen-cards-prompt.md` 的 `## 当前运行prompt版本` 下第一个 code block；`agent-persona-<room>.md` 单独注入。**只改 code block 内容，不改 Python。** 实验 prompt 请放在仓库外或 ignored 草稿里，正式版只保留 `prompts/` 下的运行模板与 example。

变量替换（`gen_cards.py` 负责）：

| 变量 | 含义 |
|------|------|
| `{agent-persona}` | agent-persona-<room>.md 全文 |
| `{existing_summary}` | 已冻结卡的 share + private 正文（去掉字段标签，多轮累积），或 `（无）` |
| `{conversation}` | 当前窗口的对话 turns，格式 `R{n}\nUser: ...\nAssistant: ...`（实际标签来自 settings） |
