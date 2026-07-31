-- v11：给 normalized adapter 建立 canonical identity、原生 provenance 与显式 room policy。
--
-- 旧消息和 session 不回填伪造的 adapter metadata；它们继续按 source_file 派生房间。
-- 新 neroli-normalized-v2 export 会写 source_sessions，并在 messages 保存原生 message id。
--
-- 应用：sqlite3 data/fragments.db < migrations/011-canonical-adapter-contract.sql

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE source_sessions (
  session_id               TEXT PRIMARY KEY,
  source                   TEXT NOT NULL,
  native_session_id        TEXT NOT NULL,
  native_parent_session_id TEXT,
  parent_session_id        TEXT,
  source_route             TEXT NOT NULL,
  room                     TEXT NOT NULL,
  created_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source, native_session_id)
);

CREATE INDEX source_sessions_parent_idx ON source_sessions(parent_session_id);

ALTER TABLE messages ADD COLUMN provider TEXT;
ALTER TABLE messages ADD COLUMN native_message_id TEXT;
ALTER TABLE messages ADD COLUMN native_parent_message_id TEXT;

CREATE UNIQUE INDEX messages_source_native_idx
  ON messages(source, native_message_id)
  WHERE native_message_id IS NOT NULL;

PRAGMA user_version = 11;

COMMIT;
