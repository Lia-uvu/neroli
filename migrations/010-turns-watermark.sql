-- v10：给 turns 原始发生层增加来源无关的变更水位。
--
-- Card Gen 的白天 watcher 只轮询此水位，不再依赖某一种 adapter 的源文件事件。
-- INSERT / DELETE 一定递增；UPDATE 仅在 turns 的业务字段实际变化时递增，避免幂等
-- 全量 ingest 的 ON CONFLICT DO UPDATE 制造虚假唤醒。
--
-- 应用：sqlite3 data/fragments.db < migrations/010-turns-watermark.sql

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE change_watermarks (
  name       TEXT PRIMARY KEY,
  revision   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO change_watermarks (name, revision)
VALUES ('turns', 0);

CREATE TRIGGER turns_watermark_after_insert
AFTER INSERT ON turns
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1,
      updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

CREATE TRIGGER turns_watermark_after_update
AFTER UPDATE ON turns
WHEN OLD.session_id IS NOT NEW.session_id
  OR OLD.source_uuid IS NOT NEW.source_uuid
  OR OLD.round IS NOT NEW.round
  OR OLD.message_seq IS NOT NEW.message_seq
  OR OLD.source_file IS NOT NEW.source_file
  OR OLD.line_no IS NOT NEW.line_no
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1,
      updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

CREATE TRIGGER turns_watermark_after_delete
AFTER DELETE ON turns
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1,
      updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

PRAGMA user_version = 10;

COMMIT;
