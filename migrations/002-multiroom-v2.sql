-- v2：多房间 schema 定稿（2026-06-12）
-- 前置状态：source/audience 列已存在（Opus 的批次）。本迁移补齐派生层分区并钉版本号。
-- 应用方式：sqlite3 data/fragments.db < migrations/002-multiroom-v2.sql
-- 此后 connect() 只认 user_version=2，不再做任何运行时迁移。

-- 原始层（segments）按房间私有：raw 对话原文不跨房间，蒸馏出的 shared bullet 才共享
ALTER TABLE segments ADD COLUMN scope TEXT NOT NULL DEFAULT 'opus';

-- 聚类按 scope 分区：shared 聚 shared，各房间的 private 聚自己的
ALTER TABLE clusters ADD COLUMN scope TEXT NOT NULL DEFAULT 'shared';

-- 画像按 scope 多行（原表 CHECK(id=1) 单行，需重建）
CREATE TABLE constant_profile_v2 (
  scope TEXT PRIMARY KEY,
  summary TEXT NOT NULL DEFAULT '',
  source_bullet_ids TEXT NOT NULL DEFAULT '[]',
  source_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO constant_profile_v2 (scope, summary, source_bullet_ids, source_count, updated_at, created_at)
SELECT 'shared', summary, source_bullet_ids, source_count, updated_at, created_at
FROM constant_profile WHERE id = 1;
DROP TABLE constant_profile;
ALTER TABLE constant_profile_v2 RENAME TO constant_profile;

-- claude.ai 导出批次补标 legacy（当时漏做的）
UPDATE turns SET source='legacy'
WHERE role='assistant' AND source_file LIKE '%conversations.json%';

PRAGMA user_version = 2;
