# 运维 · Incidents（事故剧本）

> 真实发生过的事故：症状 → 排查 → 根因 → 修复。新事故解决后**当天**补一节，
> 不写下来，下一个 session 的维护者就得重新考古一遍。
> 日常巡检入口：`bin/status.sh`（只读体检，专抓"该发生的没发生"）。

## 2026-09-18 实体裁判模型失效，索引连续软降级

**症状**：nightly 连续正常收尾，但 `rebuild index` 的 `resolve` 从 9 月 11 日起一直是
`resolve_failed`；新卡和聚类仍产出，因此表面上系统正常，实际新 tag 没有进入规范实体映射。

**根因**：`entity_resolve.judge_model` 仍指向 `gpt-5.4-mini`，Codex ChatGPT 账户已不支持
该模型。IDX-001 的失败软降级正确保护了 cards 和既有实体，但也让故障不阻断 nightly。

**修复**：裁判默认与本机配置改为成本敏感的 `gpt-5.6-luna`，保留 low reasoning、完整
batch 和保守合并门；在线备份后经共享锁重建索引，24 个待决 tag 中 5 个合并、19 个新建，
待决归零，数据库完整性和全套 143 测通过。

**教训**：`nightly done` 只表示主流程完成，不等于所有软降级模块成功；排障时必须展开
`rebuild index` 的 `resolve.status`，不能只看进程退出码。

## 2026-07-12 curator 连续三晚未真正运行

**症状**：digest / constants 停在旧状态好几天（表现为"改名成 neroli 这么大的事 curator 居然没记住"），
但没有任何报警——当时 nightly 失败还没接报警通道。

**排查路径**：`data/nightly.log` 逐晚回看 + `tree_snapshots` / `digests` 表按 night 对账
（哪晚有快照没 digest、哪晚整个缺席）。

**根因**：不是模型判断失误，是三个平庸故障刚好排成一列——
1. 改名发生在当日 nightly **之后**（旧状态多存一晚属正常）；
2. 次日出卡管线故障；
3. 第三晚 curator 因 CLI stdout 为空、输出解析崩溃（submission 其实已落盘）。

**修复**：curator 在 stdout 为空时回收已落盘的 submission；nightly 失败接
`bin/alert-local.sh`（sticky note + Apple Reminder 双通道）；补跑遗漏的整理。

**教训**：报警抓的是 presence-of-error，抓不住 absence-of-success。
`bin/status.sh` 的 curator 检查（digest 超过 28h 没产出就红灯）就是为这类死法加的。

## codex 静默失败（模式，随时可能重演）

**症状**：`cards-last-24.md` 照常刷新，但不再产出新卡；auto-cards / 实体去重 /
nightly curator 全部静默失败。极易误判为"一切正常"。

**排查**：`data/watch.log` 里找 `No such file or directory: 'codex'`；
或直接跑 `bin/status.sh`（近24h 有消息但 0 卡 = 红灯；codex 可达性单独有一项）。

**根因**：codex 二进制丢失/路径变化。ChatGPT.app 更新**会改内置路径**。
查找顺序见 `src/model.py:_codex_bin`。

**修复**：重新让 codex 上 PATH，或在 `settings.model_access.cli_cmd` 填替代 CLI。
详见 [common.md](common.md) "运行时依赖"。

## 2026-07-15 系统时区漂移导致排程误判

**症状**：凌晨看 `nightly.log` mtime 是"昨天 04:17"，差点判成"今晨那班没跑"。

**真相**：机器系统时区与 `settings.timezone` / 运维人员预期的展示时区不一致，预期中的
定时点按系统本地时间其实还没到。

**教训**：时间判断一律用 epoch/UTC 加宽容窗（status.sh 用 28h 而非 24h），
不要用"日期看起来对不对"目测。人和 agent 都会犯这个错。

**排查与处理**：先查 `/etc/localtime`、系统自动时区开关和 plist 的
`StartCalendarInterval`。launchd 按系统本地时区解释 Hour；机器睡眠错过时会在唤醒后补跑，
这与会跳过错过间隔的 `StartInterval` 不同。确认时区变化是否为部署方有意设置，再决定是
恢复系统时区还是同步调整 plist Hour；不要擅自“纠正”用户有意选择的时区。日志时间戳保留
`%z` 后缀，避免再次靠裸钟点目测。

## 2026-07-15 短对话 fork 漏检 → 同一事件出两张卡

**症状**：同一段短对话事件在 cards-last-24 里出现两张几乎相同的卡
（父/子 session 各一张，相隔很短），用户确认自己没有主动 rewind/fork。

**排查路径**：`--card` 展开两张卡 → 定位两份 JSONL（`~/.claude/projects/<room>/`）→
逐行对比 uuid 与 sessionId → 查 `session_forks` 表（空）→ 读 `refresh_session_forks`。

**根因**：desktop 里"打断回复 + 改词重发"会**静默 fork**：新建 session 文件、复制历史行、
且**重放行重新盖新 sessionId 的章**（与 Claude Code CLI 保留原 sessionId 不同），于是两个
session 各拥有一整份 turns。fork 检测靠跨 session 共享 source_uuid 行数 ≥
`ingest.fork_min_shared_turns`（当时 6）判定；这场对话只共享 4 行（纯 thinking 消息不入库），
从门槛底下漏过 → 两个 session 都被当独立会话全量出卡。

**修复**：阈值 6→2（settings.json / settings.example.json / db.py 默认值三处同步）。
安全性论证：真 uuid 跨 session 相同只可能来自复制历史；legacy normalized/test 的确定性哈希
含 `source_file`，不跨文件撞——低阈值没有误判面。`--rebuild-forks` 后补出了此前漏掉的
多对短 fork。本次重复卡按"留子删父"清理（删父卡，FK 级联零残留，
`--rebuild-cards` 刷新热层）——`sessions_needing_update` 里本就有"无卡父 + 子卡覆盖分叉前
则不重出"的防护，这是设计内的清理路径。

**遗留**：历史重复卡背账见 `data/duplicate-card-candidates.md`（含 [KEEP] 标记），待批量清理。

**教训**：门槛型判定要问"漏检的代价落在哪"——这里漏检的恰好是"整场对话都被复制"的
最短场景，正是重复卡最严重的形态。另外：用户说"我没 fork"时要信——入口在 GUI 里
隐蔽到用户自己都不知道 fork 过（打断+重发、编辑消息都算）。

## 2026-07-17 curator 成功提交后被判失败、30/60/120s 重复调用

**现象**：night-curator prompt 要求"submit 通过后不再输出别的"，agent 照做 → stdout 为空；
CLIModel 把 rc=0 + 空 stdout 当失败，按退避重试，同一晚 curator 被重复调用多次。
prompt 写得越听话，runner 越觉得它失败——协议冲突，不是模型问题。

**修复**：build_model/CLIModel 新增显式 `success_artifact` 参数，curator 传
`submission.json`；rc=0 且产物文件存在则首次调用即返回，上层照旧复验。新增
tests/runtime/test_model.py 守住两侧："有产物一次返回、无产物照旧四次重试"。实跑验收时运行时长
未出现退避。注意：当时两个房间的 `model_calls` 各 1 行只代表逻辑完成，旧审计还不能证明物理调用次数；同日后续
加固才新增 `:attempt` 逐次记录。

**教训**："成功"的判据要跟协议对齐：当 prompt 约定"完成即沉默"，runner 就不能拿 stdout
非空当成功信号，得看产物。改 prompt 的沉默约定或改 runner 的判据，二者必须动一个。

## 2026-07-17 模型调用链二次审计：空卡破坏、失败风暴与活锁误抢

**发现**：沿上案继续审计所有模型入口，临时库复现多条同级故障；部署库也发现同一
session 多次 `rewrite: -1 +0 cards` 后反复重出，watch.log 中还有大量 auto-cards 启动失败；
后者没产生模型费用，但证明“失败不冷却、每次文件事件重撞”的风暴通道真实存在。

**根因与修复**：
1. 出卡非空垃圾会解析成 `[]`，仍删旧尾卡并 commit；现改为任一窗口零卡即硬失败，旧卡
   不动。每窗口/每物理 CLI attempt 的 prompt/raw 在业务替换前独立留档，后窗失败不再把
   前窗付费输出一起 rollback。
2. auto-cards/手工批处理曾吞 session 异常并返回 0，失败也不进冷却；现任一失败返回非零，
   `gen_cards_attempt_error` 也参与调用间隔，但待处理 turns 仍以上次成功为界，冷却后可重试。
3. `success_artifact` 只在 rc=0 检查；现 timeout/非零退出后同样先认本轮新 artifact，再由
   curator 复验，submit 后 cleanup 出错不会重跑四次。
4. curator 的垃圾 stdout 会变 `{}` 后 digest 0 假成功；现 stdout fallback 也走 submitcheck，
   无合法结果硬失败。fresh 分界改为每房间上次成功 digest，而非树快照，失败夜的卡次晚仍交。
5. watcher/nightly 把年龄 >300s 的锁当死锁，但 curator 合法运行可远超 300s；现 owner 文件
   记录实际子进程 PID+token，活 PID 永不抢，死 owner 才回收，且只释放自己的 token。
6. entity judge 的残缺 JSON 曾把遗漏 tag 静默当“新实体”；现要求 batch 完整覆盖，缺项/
   重复/非法候选硬失败，并新增 `entity_resolve_attempt*` 原始审计。

**回归**：新增空输出保旧卡、后窗失败审计、timeout/nonzero+artifact、curator 无合法提交、
失败传播、失败夜 fresh 续交、活/死 owner 锁回收、entity verdict 完整性测试。修复时全套
64 测通过。

## 2026-07-17 auto-cards 跨过 nightly 时段持锁，13:11 nightly 等锁超时中断

**现象**：13:11 nightly 在 `[2/5] rebuild index` 等锁 60s 超时，`set -e` 中断于第 45 行
（rebuild-index 无 continuing 兜底），报警发出。步骤 2–5 全部未跑（当日 03:05 班次正常）。

**根因（后续校正）**：11:09 watch 触发的 `cli.py --auto-cards`（PID 92920）先拿锁，
数据库审计显示该 run 的最后模型结果到 18:23 才落下，因此无法支持“业务早已完成、
只是解释器退出挂死”的旧判断。更符合证据的解释是模型任务在 Mac 睡眠期间被整体
挂起，醒来后继续完成；13:11 nightly 只等 60 秒就放弃，把一次正常串行竞争变成整班缺席。

**处置**：进程自退、锁自动释放后，18:2x 手动补跑 nightly.sh 一班补齐索引/context/curate。

**教训**：活 owner 不能被抢锁，否则会并发写库；但 nightly 是每日成功水位，也不应
用普通前台调用的 60 秒等待上限。报警信息应带 owner PID 与持锁时长以便区分活工作和死锁。

## 2026-07-20 同型复发：auto-cards 与 nightly 竞争，睡眠拉长持锁时间

**症状**：13:11 nightly 再次在 `[2/5] rebuild index` 等锁 60 秒后中断，
报警仍显示“第 45 行意外退出”。

**排查**：对齐数据库审计与 `nightly.log`：auto-cards run 于 12:07 启动，
两个模型结果分别在 16:23、16:57 落下；nightly 于 13:11 启动并在 60 秒后放弃。
对照 07-19：当天 13:03 nightly 启动前最后一轮 auto-cards 已在 10:35 完成，因此没有锁竞争。

**根因**：故障需要两个条件同时成立：auto-cards 先拿到全局锁，且运行窗口因
模型调用/机器睡眠跨过 nightly 定时点。nightly 的 60 秒等待策略才是可稳定修复的缺口。

**修复**：nightly 共享锁改为无 deadline 排队，醒来后等 auto-cards 释锁再继续；
普通调用仍保留 60 秒上限，超时日志补 owner PID 与持锁秒数。不抢活锁，也不强杀模型任务。

**回归**：锁专项 3 测通过（活 owner 不抢、死 owner 回收、nightly 无 deadline 排队）；
全套 70 测通过。
