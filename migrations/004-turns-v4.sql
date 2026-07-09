-- v4：turns 数据完整性修复（2026-06-24）
-- 把原始层从单表 turns 拆成 messages（去重内容）+ turns（每会话出现+排序）。
--
-- 修的 bug（详见 plan / skills 决策记录）：
--   1. UNIQUE(session_id, round, role) 让一轮里第 2..N 条 assistant 互相覆盖（某旧库约 474 组受害）。
--   2. 无 source_uuid → 跨会话复制的历史（3426 个 uuid 多会话出现）无法去重。
--   3. --max-messages 80 截断文件 + assign_rounds 从切片重算 round → 同一会话多次 ingest 轮次漂移。
--   4. isCompactSummary/isApiErrorMessage/isVisibleInTranscriptOnly/裸 [Request interrupted by user] 漏进 turns。
--
-- 本迁移只重建原始层（messages + turns），内容会由 v4 ingest 全量重灌。
-- 卡层（cards/card_tags/card_raw/clusters/cluster_members/cards_fts）处理二选一：
--   * Option A（默认，本文件）：不动卡层。旧卡（基于脏 turns）继续可搜可见，
--     turn_start/turn_end 退化为对新轮次的近似「时代」指针，之后再渐进重生（或不重生）。
--     代价：零；好处：无模型开销、context-last-24 不断档。
--     ⚠️ Option A 下必须停掉 --auto-cards 的增量更新（update_session_cards 会拿旧卡的
--        turn_end 在新轮次上重喂，语义错乱）。watcher 已改为只 ingest 不出卡，
--        待显式重建卡层后再开（settings.watcher.auto_cards）。
--   * Option B（需要时手动改）：把下面 “Option B” 注释块解开，清空卡层后 --process-existing 全量重灌。
--
-- 保留不动：pipeline_runs、model_calls（审计历史）。
--
-- 应用方式（**先在副本上跑，确认无误再换 live 库**）：
--   python3 scripts/backup_cards.py            # 先备份卡层到 data/cards-v3-backup.json
--   cp data/fragments.db data/fragments-v4.db
--   sqlite3 data/fragments-v4.db < migrations/004-turns-v4.sql

-- FK pragma 在事务内是 no-op，必须在 BEGIN 之前关；CASCADE 也不能在迁移中途触发。
PRAGMA foreign_keys = OFF;

BEGIN;

-- ── Option B（默认注释掉）：清空卡层后全量重生 ──
-- FK 关着，ON DELETE CASCADE 不触发，先删子表再删父表：
-- DELETE FROM cluster_members;
-- DELETE FROM clusters;
-- DELETE FROM card_tags;
-- DELETE FROM card_raw;
-- DELETE FROM cards;
-- DELETE FROM cards_fts;
-- ── Option A：以上全部跳过，卡层原样保留 ──

-- 拆旧 turns，建 messages + 新 turns + 三个索引
DROP TABLE IF EXISTS turns;
DROP TABLE IF EXISTS messages;

CREATE TABLE messages (
  source_uuid  TEXT PRIMARY KEY,
  role         TEXT NOT NULL,
  speaker      TEXT NOT NULL,
  text         TEXT NOT NULL,
  timestamp    TEXT,
  parent_uuid  TEXT,
  source       TEXT NOT NULL DEFAULT 'opus',
  model        TEXT,
  has_image    INTEGER NOT NULL DEFAULT 0,
  image_count  INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE turns (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  source_uuid  TEXT NOT NULL REFERENCES messages(source_uuid),
  round        INTEGER NOT NULL,
  message_seq  INTEGER NOT NULL,
  source_file  TEXT,
  line_no      INTEGER,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, source_uuid)
);
CREATE INDEX turns_session_order_idx ON turns(session_id, round, message_seq, line_no);
CREATE INDEX turns_source_uuid_idx   ON turns(source_uuid);
CREATE INDEX messages_timestamp_idx  ON messages(timestamp);

PRAGMA user_version = 4;

COMMIT;

PRAGMA foreign_keys = ON;
