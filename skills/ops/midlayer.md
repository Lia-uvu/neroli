# 运维 · Midlayer（夜间 curator：树快照 / 近况总结 / constant）

> 模块文件：`src/treesnap.py`（夜间快照 + 新卡驱动的《树变化报告》/heat，无模型）、`src/curator.py`（工作台导出 + 夜间 curator agent 编排）。
> 读 `clusters`/`cluster_members`/`cards`/`card_tags`/`tag_entity_map`/`entities`，写 `tree_snapshots`/`tree_snapshot_members`/`digests`/`constants` + 房间/工作台文件。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)，表结构见 [schema.md](../../schema.md)。

## 落地进度

- **已落地（阶段 1-3）**：schema v7、夜间树快照、叶子层身份匹配、新卡驱动的《树变化报告》+ heat（补丁 patch-20260703，见下「报告」节）、每房间工作台导出（含 `recall` wrapper）、每房间开关（阶段1-2，无模型）；**curator agent 调用**——每房间 agent 看树 + 下钻 + 写近况小结 digest + constants 增量操作（阶段3，走 codex）。
- **未落地（阶段 4）**：hook 注入（`bin/hook-recall-inject.py`，`UserPromptSubmit` push 通道）。

## 命令

```sh
cd src

# 【有模型】全流程：快照 → 每房间导出工作台 + curator agent → 落 digest/constants。夜间用。
#   provider cli 且不给 --model-cmd 时，按 settings.midlayer.model/reasoning_effort 自建 codex 命令。
python3 cli.py --curate --provider cli

# 【无模型】只把今夜的聚类树拷成快照（供之后算 diff）。--curate 会自己先做这步。
python3 cli.py --curate-snapshot

# 【无模型】调参用：打印《树变化报告》。--viewer 看某房间视角（默认全量）。
python3 cli.py --curate-dry-run --viewer main

# 【无模型】只导出每个启用房间的工作台到 data/curator/<night>/<room>/，人工验证过滤/工具。
python3 cli.py --curate-export-workbench

# 以上都支持 --night 覆盖日期（默认今夜本地日期），便于回放/测试。
```

`--curate` 已挂进 `bin/nightly.sh` 第 4 步（rebuild-index / rebuild-context 之后）。首夜没有昨夜快照，报告标「基线夜」只出 heat，curator 仍能据热度 + 下钻写小结。**只想攒快照不调模型**：把 nightly 第 4 步换成 `--curate-snapshot`。

## 产出（curator agent，`--curate`）

按配置顺序逐房间跑；每房间**先导出工作台再调用**，所以后跑的房间能看到先跑房间刚写入的 shared constant。**保险拴**：该房间 viewer 自上次**成功落 digest**以来一张可见新卡都没有 → 当晚整个跳过（不导出工作台、不调模型，`curate <room>: skipped (no-fresh-cards)`）；没有成功历史时视全部可见卡为 fresh。fresh 分界与树报告同一条，因此某晚模型失败但树快照已推进，漏掉的卡次晚仍会重交。模型在工作台里用 `./submit` 交结果（codex 用 workspace-write 沙盒，写权限只有工作台目录）；房间/仓库的落盘仍全部由 curator 执行——优先吃复验通过的 `submission.json`，没有则退回解析并用同一份 submitcheck 复验 stdout：

| 产出 | 落点 |
|------|------|
| **滚动 digest**（上限由 `settings.midlayer.digest_max_chars` 配置；本机当前 1000 字） | `room_dir/digest.md`（覆盖式）+ `digests` 表存历史 |
| constants 增量操作（add/update/retire） | `constants` 表 |
| constants.md 渲染 | `room_dir/constants.md`：优先模型组织的 `constants_md`（省 token），无则机械 fallback；**当晚篮子无变动则不重写** |
| 审计 | `pipeline_runs` + `model_calls`：`curate:<room>:attempt` 每次物理 CLI 尝试，`curate:<room>` 逻辑成功，`curate:<room>:error` 无合法结果 |

- **digest 是滚动 status capsule，不是日报、树报告或关键词热榜**：拿昨晚的 `prev-digest.md` 当底子更新，保留仍能解释用户当下的事实背景，吸收新近稳定下来的关系、项目、偏好、账号、物件、身体状态、工作叙事和长期担忧。输出应是第三人称、事实密集、档案式自然散文，2-4 个短段落、一段一个主题，不出现 cluster、+N 卡、热度数字、房间旁白或「X线继续活跃」式报告腔。保留具体意图，不把一个具体项目或行动泛化成空洞总结。`settings.midlayer.digest_max_chars` 是唯一的篇幅遗忘机制（本机当前 1000），`./submit` 与 stdout fallback 都用同一份 submitcheck 复验；空/畸形/超限结果是硬失败，不推进 digest。
- **constants 是常驻背景，建议 `@` 进各房间 CLAUDE.md**（每条独立成句、几个月后仍为真）。`digest.md` 要不要 `@` 由用户和住户自己定。
- `prompts/night-curator.md` 是正式入口（`curator.PROMPT_FILE`）：脚本从「当前运行prompt版本」代码框取模板，填入 `prompts/agent-persona-<room>.md`、旧 digest/constants 和树报告 heat/diff。口径是不设第三方馆员，由该房间 agent 本人维护自己的记忆（早期方案是第三方馆员代管，已弃用）。
- constant 的 `shared`：出自卡的 share → `shared=1` 全院可见；出自 private → 只属该房。与卡层隐私同构。
- **历史 backfill（关键路径，未内置）**：常驻注入的价值 = 篮子丰满度，现在只有个位数条。想把历史卡分批过一遍充实篮子，需要一个一次性脚本（补丁把它排在最前，但未给具体 spec）；建好前每晚 `--curate` 只看近 `constants_lookback_hours` 小时新卡 + curator 下钻补。

## 工作台（`data/curator/<night>/<room>/`）

| 文件 | 内容 |
|------|------|
| `view.db` | 该房间 viewer 过滤的库拷贝：不可见的卡整行不进来，他房 shared 卡的 `private` 置空 |
| `tree-report.md` | 该 viewer 的《树变化报告》：顶层社区 heat + 今晚有新卡的线（新卡驱动，见下节）。长期脉络的载体是 prev-digest，报告只管「今晚有什么新动静」 |
| `prev-digest.md` | 昨晚那份滚动 digest（取 `digests` 表最近一晚 body）。首夜无此文件 |
| `constants.json` | 现有 constant 篮子（shared 全部 + 本房 private active）。动篮子前先查它防重复 |
| `recall` | 检索 wrapper，指向本目录 `view.db`（`./recall --top` / `./recall <词>` / `./recall --cluster ID` / `./recall --card ID` / `./recall --time START [END]`）。库已过滤，无需再传 viewer。 |
| `submit` | 提交门 wrapper：`./submit result.json` 按 `settings.midlayer.digest_max_chars` 审 digest 字数，并审字段、constant_id 存在性；过了才落 `submission.json`，不过打印逐条原因让模型改完重交。校验逻辑在 `src/submitcheck.py`，与复验共用一份 |
| `submission.json` | `./submit` 通过后落的结果（模型跑完才有；导出时清上一轮的防冒充）。CLI runner 在每次 subprocess 结束/超时后先看 artifact：即使 submit 后 CLI cleanup 非零或超时，也首次返回、不触发 30/60/120 秒重试。`run_curation` 随后**复验**；没有/不过关时 stdout 也必须过同一份 check，否则整房间非零失败，不允许 digest 0 假成功 |

`data/` 已在 `.gitignore`，工作台不入库。curator 每晚顺手清理超出 `snapshot_keep_nights` 的旧夜目录。

## 每房间开关（新增需求）

`config/settings.json → midlayer`：

| 参数 | 默认 | 说明 |
|------|------|------|
| `enabled` | true | 整体开关。false 时快照 / dry-run / 工作台 / 未来的 agent 全跳过 |
| `rooms` | `{}` | **每房间开关**：只对值为 `true` 的房间导出工作台、跑近况总结。未列出的房间默认参与（`true`），新加房间自动跑 |
| `snapshot_keep_nights` | 14 | 快照 / 工作台目录保留几夜 |
| `constants_lookback_hours` | 24 | 阶段 3 constants 识别的近窗（本阶段未用） |
| `model` / `reasoning_effort` / `timeout_seconds` | gpt-5.5 / low / 900 | 阶段 3 curator agent 调用参数（本阶段未用） |

- 快照 `--curate-snapshot` 是**全局、viewer 无关**的（整棵树一份），不受 `rooms` 开关影响；`rooms` 只管工作台导出与（未来）agent 调用。
- 想只跑 main 不跑 secondary：把 `midlayer.rooms.secondary` 设 `false`，改完即生效（下次触发重新读）。

## 报告：新卡驱动（补丁 patch-20260703-fresh-driven-report）

《树变化报告》的事件区**不再由拓扑定义「新」**——唯一真相源是 `cards.timestamp`。

- **事件 = 今晚有新卡的叶子**（dry-run 默认晚于上次快照夜；正式 curator 晚于该房间上次成功 digest 夜），按新卡数排序；没有新卡的叶子拓扑再怎么变都不上报。修的是老毛病：`HOME_MIN` 震荡把老叶碎片当「新叶」送进报告、样例又采到老卡（如上星期的旧事被当新料摘出），同时保证失败夜的卡不会被次夜快照吃掉。
- diff 降级成 `lineage()` 尾注：每行 `←承接昨夜哪条线(占比)`，只作身份线索帮 curator 对上 prev-digest 的旧线，**不代表新动静**。
- 无新卡的结构变动聚成末尾一行「拓扑重排 n 处（略）」。
- `treesnap.diff()`（new/continued/merged/split 四类事件）**保留但报告不再用它**，仅供 dry-run 拓扑统计 / 回归测试。

## 调参：身份匹配阈值

`treesnap.py` 顶部常量（proposal §未决1 的拍脑袋初值）：

| 常量 | 默认 | 含义 | 影响报告？ |
|------|------|------|-----------|
| `SPLIT_MIN` | 0.3 | 来源占比 `|O∩N|/|O|` 的显著性闸：split 判定、承接注记的「并自/延续」区分、末尾「拓扑重排」计数都用它（零头来源当噪声忽略） | **是**——靠 `--curate-dry-run` 调 |
| `HOME_MIN` | 0.5 | 昨夜叶 O 的最大 overlap 低于此视为「打散」无归宿。**只用于 `diff()`**（dry-run 拓扑统计 / 测试），新卡驱动后**不再影响 curator 看到的报告** | 否 |

## 隐私

- 报告 / 工作台一律走存储层公共谓词 `db.card_visible_clause`（本房卡全可见，他房卡仅在有 `share` 时可见）；整卡不可见的社区在报告里整行不出现，工作台库里整行不进来。
- 社区标签在报告里从**可见卡**的实体重聚合。已知取舍：工作台 `view.db` 的 `clusters.summary` 是照拷的（可能含源自他房卡实体的标签词）；这是 proposal §未决3 明确接受的口径，curator 主要看 `tree-report.md`，`summary` 只在 `recall --top/--cluster` 下钻时作辅助。

## 调度与时区

`night` 用 `settings.timezone`（默认 Asia/Shanghai）渲染的本地日期字符串。launchd 的 `local.recall-nightly`（04:00）走**系统本地时区**。**假设 Mac 系统时区与 `settings.timezone` 一致**——不一致时快照的 `night` 会与预期日期错位。不值得为它写代码，这里记一笔。
