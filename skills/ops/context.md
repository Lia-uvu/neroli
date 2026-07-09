# 运维 · Context（最近窗口工作记忆）

> 模块文件：`src/context.py`（+ `src/tempo.py` 时间透镜原型）。读 `cards`/`turns`，写各房间目录下的 `context-last-24.md`（文件，非表）。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 重建所有房间的 context 文件
bin/cli.py --rebuild-context

# 只重建某个房间的
bin/cli.py --rebuild-context --context-room main
```

输出路径：`config/rooms.json` 里每个房间的 `room_dir/context-last-24.md`。出卡后默认随 `finalize_card_updates` 一起重建。

## 调参（config/settings.json → `context`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `lookback_hours` | 24 | context 窗口时长 |
| `max_cards` | 80 | context 最多显示几张卡；只写 theme，不写 share/private 正文 |

跨房间可见性：context 只展示 `theme`；某卡 `room != viewer` 时带 `[room]` 标记，且只在该卡 `share` 非空时才跨房间出现（private-only 卡不跨房间）。
