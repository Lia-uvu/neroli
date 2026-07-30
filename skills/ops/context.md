# 运维 · Context（最近窗口事件卡原料）

> 模块文件：`src/context.py`（+ `src/tempo.py` 时间透镜原型）。读 `cards`/`turns`，写各房间目录下的 `cards-last-24.md`（文件，非表）。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 重建所有房间的 cards 文件
bin/cli.py --rebuild-cards

# 只重建某个房间的
bin/cli.py --rebuild-cards --context-room main
```

输出路径：`config/rooms.json` 里每个房间的 `room_dir/cards-last-24.md`。`--rebuild-context` 保留为兼容别名。出卡后默认随 `finalize_card_updates` 一起重建，并触发 last24 summarizer。

日期和每条卡片前的时间按 `config/settings.json` 顶层的 `timezone` 渲染；每次重建都会重新读取该设置，无需重启 watcher。该时区下的当日标题会显示为 `--YYYY-MM-DD（今天）--`。

## 调参（config/settings.json → `context`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `lookback_hours` | 24 | cards 窗口时长 |
| `max_cards` | 80 | cards 文件最多显示几张卡；只写 headline，不写 share/private 正文 |

跨房间可见性：cards 文件只展示 `headline`；某卡 `room != viewer` 时带 `[room]` 标记，且只在该卡 `share` 非空时才跨房间出现（private-only 卡不跨房间）。

`src/last24.py` 把这份 headline 原料复制进 viewer-filtered 工作台，并提供 curator 同款 `./recall` 与 `./submit`。`last24_summary.agent_name` 指定的 summarizer 必须把结果提交为 `{"summary": "..."}`；工作台门审和主进程复验共用 `summarycheck.py`，正文硬上限由 `last24_summary.max_chars` 配置（默认 700 字）。合法结果写 `room_dir/summary-last-24.md`；失败保留上一版。

```sh
bin/cli.py --summarize-last24
bin/cli.py --last24-export-workbench   # 只导出原料和工具，不调模型
```
