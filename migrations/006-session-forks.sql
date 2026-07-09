-- v6：会话 fork 关系层（2026-06-28）
--
-- Claude Code 在撤回/重发或手动 fork 时，可能创建一个新 session JSONL，
-- 但把旧 transcript 原样复制进去；这些复制行保留原 source_uuid。此表记录
-- child -> parent 的推断关系和 fork 点，供出卡只处理 delta。
--
-- 应用：sqlite3 data/fragments.db < migrations/006-session-forks.sql

CREATE TABLE IF NOT EXISTS session_forks (
  child_session_id   TEXT PRIMARY KEY,
  parent_session_id  TEXT NOT NULL,
  fork_round         INTEGER NOT NULL,
  parent_fork_round  INTEGER NOT NULL,
  delta_start_round  INTEGER NOT NULL,
  shared_turns       INTEGER NOT NULL,
  child_turns        INTEGER NOT NULL,
  parent_turns       INTEGER NOT NULL,
  child_shared_ratio REAL NOT NULL,
  detected_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS session_forks_parent_idx ON session_forks(parent_session_id);

PRAGMA user_version = 6;
