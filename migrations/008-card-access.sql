-- v8：卡片取用日志（read-heat）——重量不在写入时标，用「被重新拿起」度量（2026-07-11）
--
-- 写入端只有一处：retrieval CLI 的 --card 分支（列表扫过不算，展开全文才算）；
-- RECALL_NO_LOG=1 跳过写入，批量脚本/排查自觉带上防污染。curator 工作台的 recall
-- 指向自己的 view.db 快照，天然进不了主库，无需排除。
-- 读端：treesnap.render_report 聚合成顶层社区取用计数 + top 卡清单，随树报告喂 curator，
-- 让「有动静」除了写入热（新卡）之外多一个通道：取用热（旧卡被反复拿起）。
-- ts 由写入方给 UTC ISO（与 cards.timestamp 可比）；DEFAULT 只是兜底。
-- 不动其余各层。
-- 应用：sqlite3 data/fragments.db < migrations/008-card-access.sql

CREATE TABLE IF NOT EXISTS card_access (
  card_id TEXT NOT NULL,
  viewer  TEXT NOT NULL,                           -- room slug（如 main / secondary）
  source  TEXT NOT NULL DEFAULT 'search',          -- search | 其他来路（聚合只认 search）
  ts      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS card_access_ts_idx   ON card_access(ts);
CREATE INDEX IF NOT EXISTS card_access_card_idx ON card_access(card_id, ts);

PRAGMA user_version = 8;
