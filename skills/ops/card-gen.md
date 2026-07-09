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
```

## 调参（config/settings.json → `card_gen`）

改完即生效，无需重启 launchd。

| 参数 | 默认 | 说明 |
|------|------|------|
| `min_new_turns` | 5 | 触发出卡的全局新 turns 最低门槛 |
| `min_interval_minutes` | 30 | 距上次出卡的最小间隔（分钟） |
| `min_first_session_turns` | 4 | 无卡新 session 首次出卡的最低 turns 数；已有卡的 session 不受此限制 |
| `max_sessions_per_trigger` | 10 | 每次触发最多处理几个 session |
| `model` | gpt-5.5 | 模型名 |
| `reasoning_effort` | low | codex exec 的 reasoning effort |
| `turn_cap` | 1600 | 单条 turn 最大字符数（截断） |
| `init_rounds` | 14 | 首次窗口大小（轮） |
| `grow_rounds` | 14 | 后续每次追加的轮数 |

## 模型配置

默认模型：GPT-5.5 via Codex CLI，`reasoning_effort=low`。

`--auto-cards` 从 settings.json 读 `model` 和 `reasoning_effort`，自动构建 codex 命令。手动跑可覆盖：

```sh
bin/cli.py --process-existing --skip-processed --provider cli --model gpt-5.5
```

三后端（cli / ollama / api）实现见 `src/model.py`。

## prompt

出卡 prompt 模板在 `prompts/gen-cards-prompt.md` 的 `## 当前运行prompt版本` 下第一个 code block；`agent-persona-<room>.md` 单独注入。**只改 code block 内容，不改 Python。** 实验 prompt 请放在仓库外或 ignored 草稿里，正式版只保留 `prompts/` 下的运行模板与 example。

变量替换（`gen_cards.py` 负责）：

| 变量 | 含义 |
|------|------|
| `{agent-persona}` | agent-persona-<room>.md 全文 |
| `{existing_summary}` | 已冻结卡的 share + private 正文（去掉字段标签，多轮累积），或 `（无）` |
| `{conversation}` | 当前窗口的对话 turns，格式 `R{n}\nUser: ...\nAssistant: ...`（实际标签来自 settings） |
