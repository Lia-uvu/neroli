# 运维 · Search（检索）

> 模块文件：`src/retrieval.py`（只读门面 + CLI）。库函数只读；唯一写例外是 CLI `--card` 往 `card_access` 记取用日志（`RECALL_NO_LOG=1` 跳过）。入口 wrapper 由安装环境提供。
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

# 卡片 + 原始对话全文（turns 回溯，仅同房间的卡）
python3 src/retrieval.py --viewer main --card CARD_ID --turns

# 时间范围
python3 src/retrieval.py --viewer main --time 2026-06-01 2026-06-24

# 指定房间
python3 src/retrieval.py --viewer secondary 关键词
```

## 一次调用拿全（agent 用户的回合经济）

- `关键词 --expand N`：搜完自动展开前 N 条命中全文（每张计一笔 card_access）。
- `--card ID --around`：列同 session 全部卡（turn 区间＋当前卡标记），可见性同 card_visible_clause。
- `关键词 --sem`：向量语义检索（`retrieval.semantic_search`）。卡片向量只读 `.emb_cache`（Leiden 建树产物），query 向量缺缓存时打一次 embedding API（settings.embedding）。**隐私**：同房间用全文向量；跨房间只用 headline+share 向量——share 向量由 `scripts/backfill_share_vecs.py` 预热（幂等，新卡出现后需重跑；尚未挂 nightly），缺向量的卡静默跳过。

## 匹配语义（2026-07-15 起）

多词查询两遍走：先要求全部主词命中（FTS 隐式 AND），不满 limit 再按任一词命中补位。查询侧分词经 `db._jieba_query_exprs` 做子词展开对齐索引（lcut 把「生日礼物」切成整词时，同时接受「生日"＋"礼物」子词组合命中），纯标点词条丢弃。每条 FTS 命中带 `snippet()` 上下文片段（`CardRef.why`，CLI 里显示为 `⌙` 行）；tag 命中标明命中的 tag。jieba 初始化日志已静音（`db.py` 里 `setLogLevel`）。

## 隐私边界

关键词检索的匹配范围（2026-07-15 起）三路合并：`{headline share}` 对全部可见卡；`{private}` 只对与 viewer 同房间的卡参与匹配（跨房间照旧不进匹配范围）；tags 走 `card_tags` 子串匹配、排在 FTS 命中后。`private` 全文仅在 `--card` 且 viewer 与 `cards.room` 相同时展示；`--turns` 原文（完整 transcript，隐私等级高于 share）同样仅限同房间。详见 [schema.md](../../schema.md) 的 `cards_fts` 一节。

已知边界（既有设计，非 bug）：tags 可能由 private 内容生成，而 tags 跨房间可见——跨房间关键词可经 tag 命中一张 private-only 内容的卡的 headline。要收紧需改 card_detail 与 search 的 tag 路径。
