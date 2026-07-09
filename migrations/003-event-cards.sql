-- v3：事件卡架构（2026-06-23）
-- 把派生层从 bullet_points + segments 换成「事件卡」。
-- 数据结构依据：lab 的 design.html（四层存储）+ 早期「搜索建模 & 数据结构」讨论备忘。
--
-- 现状前提：本库派生层全空（segments/bullet_points/clusters/cluster_items/constant_profile/model_calls 均 0 行），
--           只有 turns(ground truth, 23621) 有数据。所以本迁移是「拆旧表 + 建新表」，无数据搬运。
--
-- 变更要点：
--   * 弃 bullet_points + segments（旧派生层，已空）。
--   * cards：事件卡，一张卡=一个事件(turn 区间)，卡内同时含 share(全院可见)+private(仅 room 可见)。
--            只放搜索/展示必要字段；room 做隐私过滤；model 存官方格式原样。
--   * card_tags：entity overlap + 二阶共现的源（原 lab 独立 cooccur.db，合并入此库）。
--   * card_raw：模型原始输出 + 备份元数据，与 cards 分开存（cards 表保持精简）。
--   * clusters：精简到 cluster_id(TEXT) + summary。时间范围/卡数从 cluster_members join cards 现算，不存。
--   * cluster_members：card↔cluster 多对多，role=primary/secondary（重叠归属）。
--   * model_calls.topic_id 原 FK→segments，弃表后悬空，重建去 FK，列改记 card_id。
--   * constant_profile：本步不建（profile 当注入层、结构待设计，见备忘）。旧空表一并 DROP。
--
-- 暂未做（明天待办）：clusters 层级(parent_id/level) / embedding 向量索引（本地无 sqlite-vec，先只 FTS+timestamp）。
--
-- 保留不动：turns、pipeline_runs。
--
-- 应用方式（**只在副本上跑，勿碰 live 库**）：
--   cp data/fragments.db data/fragments-v3.db
--   sqlite3 data/fragments-v3.db < migrations/003-event-cards.sql

PRAGMA foreign_keys = OFF;

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 拆旧派生层（FTS / 触发器 / 表，均空）
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS bullet_points_ai;
DROP TRIGGER IF EXISTS bullet_points_ad;
DROP TRIGGER IF EXISTS bullet_points_au;
DROP TABLE   IF EXISTS bullet_points_fts;
DROP TABLE   IF EXISTS cluster_items;
DROP TABLE   IF EXISTS clusters;
DROP TABLE   IF EXISTS bullet_points;
DROP TABLE   IF EXISTS segments;
DROP TABLE   IF EXISTS constant_profile;

-- ---------------------------------------------------------------------------
-- 2. model_calls 去掉对 segments 的悬空外键（审计表，行全保留；现 0 行）
-- ---------------------------------------------------------------------------
CREATE TABLE model_calls_v3 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  session_id TEXT,
  card_id TEXT,                 -- 原 topic_id(→segments)，改记 card_id；历史行置 NULL
  step TEXT NOT NULL,
  prompt TEXT NOT NULL,
  raw_output TEXT NOT NULL,
  parsed_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO model_calls_v3 (id, run_id, session_id, card_id, step, prompt, raw_output, parsed_json, created_at)
SELECT id, run_id, session_id, NULL, step, prompt, raw_output, parsed_json, created_at FROM model_calls;
DROP TABLE model_calls;
ALTER TABLE model_calls_v3 RENAME TO model_calls;

-- ---------------------------------------------------------------------------
-- 3. cards（派生层正文，只留搜索/展示字段）
-- ---------------------------------------------------------------------------
CREATE TABLE cards (
  card_id    TEXT PRIMARY KEY,        -- "session前缀#序号"
  session_id TEXT NOT NULL,
  turn_start INTEGER,
  turn_end   INTEGER,
  theme      TEXT NOT NULL DEFAULT '',
  share      TEXT NOT NULL DEFAULT '', -- 全院可见
  private    TEXT NOT NULL DEFAULT '', -- 仅 room 可见
  timestamp  TEXT,                     -- 原始对话发生时间，唯一时间字段
  room       TEXT NOT NULL DEFAULT 'main', -- room 名，隐私过滤用
  model      TEXT                      -- 官方 API 格式原样，如 claude-opus-4-6-20250623
);
CREATE INDEX cards_session_idx ON cards(session_id);
CREATE INDEX cards_time_idx    ON cards(timestamp);

-- ---------------------------------------------------------------------------
-- 4. card_tags（entity overlap + 二阶共现的源）
-- ---------------------------------------------------------------------------
CREATE TABLE card_tags (
  card_id TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  tag     TEXT NOT NULL,
  PRIMARY KEY (card_id, tag)
);
CREATE INDEX card_tags_tag_idx ON card_tags(tag);

-- ---------------------------------------------------------------------------
-- 5. card_raw（原始输出 + 备份元数据，与 cards 分开）
-- ---------------------------------------------------------------------------
CREATE TABLE card_raw (
  card_id     TEXT PRIMARY KEY REFERENCES cards(card_id) ON DELETE CASCADE,
  raw         TEXT,                    -- 模型原始输出
  label       TEXT,                    -- batch / realtime / backfill
  source_file TEXT,                    -- 来源文件路径
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP  -- pipeline 处理时间
);

-- ---------------------------------------------------------------------------
-- 6. clusters（精简：只 id + summary，时间范围/卡数现算）
-- ---------------------------------------------------------------------------
CREATE TABLE clusters (
  cluster_id TEXT PRIMARY KEY,
  summary    TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- 7. cluster_members（card↔cluster，role 区分主/次归属，支持重叠）
-- ---------------------------------------------------------------------------
CREATE TABLE cluster_members (
  card_id    TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'primary',  -- primary / secondary
  PRIMARY KEY (card_id, cluster_id)
);
CREATE INDEX cluster_members_card_idx    ON cluster_members(card_id);
CREATE INDEX cluster_members_cluster_idx ON cluster_members(cluster_id);

-- ---------------------------------------------------------------------------
-- 8. cards 全文索引（standalone，Python 侧 jieba 分词后写入，不 content-sync）
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE cards_fts USING fts5(
  card_id UNINDEXED,
  theme, share, private
);

PRAGMA user_version = 3;

COMMIT;
