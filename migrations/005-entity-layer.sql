-- v5：v2 索引层第一步——实体去重层 + 社区层次（2026-06-26）
-- 设计见 docs/design-leiden.html（Stage 2 实体层、Stage 4/5 层次社区）。
--
-- 改动：
--   1. 新增 entities（规范实体节点）+ tag_entity_map（tag→实体，含别名）。
--      card_tags 不动；脏 tag 的同义词合并已在 card_tags 上落地（见 tag_wash.py），
--      本表把当前规范 tag 提升为实体，并保留别名映射供未来在线去重复用。
--   2. clusters 加 parent_cluster_id + level，支持递归 Leiden 的变深度层次。
--      旧的平铺 clusters 行 level 默认 0、parent 为 NULL，语义不变。
--
-- 不动：cards / card_tags / card_raw / cluster_members / cards_fts / 原始层 / 审计层。
-- 应用：sqlite3 data/fragments.db < migrations/005-entity-layer.sql
--       之后跑 python scripts/build_entities.py 填充 entities/tag_entity_map。

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS entities (
  entity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_name TEXT NOT NULL UNIQUE,
  name_embedding BLOB,
  created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tag_entity_map (
  tag       TEXT PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS tag_entity_map_entity_idx ON tag_entity_map(entity_id);

ALTER TABLE clusters ADD COLUMN parent_cluster_id TEXT REFERENCES clusters(cluster_id) ON DELETE CASCADE;
ALTER TABLE clusters ADD COLUMN level INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS clusters_parent_idx ON clusters(parent_cluster_id);

PRAGMA user_version = 5;
