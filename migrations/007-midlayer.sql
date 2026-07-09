-- v7：中期层（馆员 / Midlayer）——夜间树快照 + 近况总结 + constant 篮子（2026-07-02）
-- 设计见 docs/proposal-20260702-midlayer-tree-privacy.md「落地架构方案」。
--
-- 新增四表，互不牵动现有五模块：
--   1. tree_snapshots / tree_snapshot_members —— 每晚把当前 clusters/cluster_members
--      原样拷进快照，供 treesnap 做「今夜 X = 昨夜 Y」的叶子层身份匹配与 diff。
--   2. digests —— 每房间每晚的近况小结（body 落库存历史，房间文件另存）。本次先建表不写。
--   3. constants —— 只说过一次但永久有效的事实篮子（生日等）。本次先建表不写。
--
-- night 为本地日期字符串（如 2026-07-03）。审计复用 pipeline_runs / model_calls。
-- 不动：原始层 / cards / 索引层 / 审计层。
-- 应用：sqlite3 data/fragments.db < migrations/007-midlayer.sql

-- 夜间树快照：每晚一份，cluster_id 在同一夜内唯一。
CREATE TABLE IF NOT EXISTS tree_snapshots (
  night             TEXT NOT NULL,               -- 本地日期字符串
  cluster_id        TEXT NOT NULL,
  parent_cluster_id TEXT,
  level             INTEGER NOT NULL DEFAULT 0,
  summary           TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (night, cluster_id)
);
CREATE INDEX IF NOT EXISTS tree_snapshots_night_idx  ON tree_snapshots(night);
CREATE INDEX IF NOT EXISTS tree_snapshots_parent_idx ON tree_snapshots(night, parent_cluster_id);

-- 快照成员：card↔cluster，role 区分 primary/secondary（与 cluster_members 同构）。
CREATE TABLE IF NOT EXISTS tree_snapshot_members (
  night      TEXT NOT NULL,
  cluster_id TEXT NOT NULL,
  card_id    TEXT NOT NULL,
  role       TEXT NOT NULL DEFAULT 'primary',
  PRIMARY KEY (night, cluster_id, card_id)
);
CREATE INDEX IF NOT EXISTS tree_snapshot_members_night_idx   ON tree_snapshot_members(night);
CREATE INDEX IF NOT EXISTS tree_snapshot_members_cluster_idx ON tree_snapshot_members(night, cluster_id);
CREATE INDEX IF NOT EXISTS tree_snapshot_members_card_idx    ON tree_snapshot_members(night, card_id);

-- 近况小结：每房间每晚一份。body 存历史；房间文件（digest.md）另由 curator 覆盖式写。
CREATE TABLE IF NOT EXISTS digests (
  night      TEXT NOT NULL,
  room       TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  model      TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (night, room)
);

-- constant 篮子：只说过一次但永久有效的事实。shared=1 全院可见；否则仅 room 可见。
CREATE TABLE IF NOT EXISTS constants (
  constant_id    TEXT PRIMARY KEY,
  room           TEXT NOT NULL,
  shared         INTEGER NOT NULL DEFAULT 0,   -- 0/1；同卡层 share/private 隐私模型
  content        TEXT NOT NULL,
  source_card_id TEXT,
  status         TEXT NOT NULL DEFAULT 'active', -- 'active' | 'retired'
  first_seen     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS constants_room_idx   ON constants(room, status);
CREATE INDEX IF NOT EXISTS constants_shared_idx ON constants(shared, status);

PRAGMA user_version = 7;
