# 运维 · Search（检索）

> 模块文件：`src/retrieval.py`（只读门面 + CLI）。读所有表，不写。入口 wrapper 由安装环境提供。
> 模块全貌见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

## 命令

```sh
# 默认关键词搜
python3 src/retrieval.py --viewer main 关键词

# 顶层社区
python3 src/retrieval.py --viewer main --top

# 展开某社区的卡片时间线
python3 src/retrieval.py --viewer main --cluster CL_ID

# 展开完整卡片
python3 src/retrieval.py --viewer main --card CARD_ID

# 时间范围
python3 src/retrieval.py --viewer main --time 2026-06-01 2026-06-24

# 指定房间
python3 src/retrieval.py --viewer secondary 关键词
```

## 隐私边界

关键词检索只搜 `{theme share}`，不搜 `private`，保证关键词入口跨房间安全。`private` 仅在 `--card` 且 viewer 与 `cards.room` 相同时展示。详见 [schema.md](../../schema.md) 的 `cards_fts` 一节。
