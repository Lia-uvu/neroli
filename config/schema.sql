-- schema v11（2026-07-31，canonical adapter identity + explicit local room routing policy）
-- schema v10（2026-07-31，turns 来源无关变更水位，供白天 Card Gen watcher 轮询）
-- schema v9（2026-07-17，事件卡字段 theme 更名为 headline，含 cards_fts）
-- schema v8（2026-07-11，卡片取用日志 card_access：read-heat，重量用「被重新拿起」度量）
-- schema v7（2026-07-02，中期层/馆员：树快照 + 近况总结 + constant 篮子）。设计见 docs/proposal-20260702-midlayer-tree-privacy.md。
-- schema v6（2026-06-28，会话 fork 关系层）
-- schema v5（2026-06-26，Leiden 索引层：实体去重 + 社区层次）。设计依据 docs/design-leiden.html。
-- schema v4（2026-06-24，turns 数据完整性修复：messages/turns 拆分）
-- 此文件只用于创建全新数据库。已有库的版本升级走 migrations/ 下的编号 SQL，
-- connect() 校验 user_version，不做运行时迁移。
-- 数据结构依据：lab 的 design.html（四层存储）+ 早期「搜索建模 & 数据结构」备忘。
--
-- 核心概念：
--   source/room = v2 source 是 adapter namespace，旧来源仍是 user/model-alias 历史标签；
--                 room 是本机 policy 决定的空间归属（cards.room: rooms.json 房间名）
--   卡内分层   = 一张事件卡同时含 share（全院可见）与 private（仅 room 可见）两段。
--                audience 不是行级属性，而是卡内字段。
--   搜索/展示  = 搜索在 card 层（FTS/time），沿 Leiden community 下钻到卡片。
--   实体层     = card_tags 是实体提取粗稿；entities/tag_entity_map 是去重后的规范实体（v2 索引层）。
--   层级       = clusters 支持层次（parent_cluster_id/level），叶社区落 cluster_members（v2）。
--
-- 暂未含（待设计）：profile 画像注入层；embedding 向量索引。（constant 篮子已在 v7 落表）

PRAGMA journal_mode = WAL;
PRAGMA user_version = 11;

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  prompt_file TEXT NOT NULL,
  source_files TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  session_id TEXT,
  card_id TEXT,
  step TEXT NOT NULL,
  prompt TEXT NOT NULL,
  raw_output TEXT NOT NULL,
  parsed_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- adapter session provenance。adapter 只声明来源事实；room 是本机私有
-- ingest.source_routes policy 的解析结果，不能由公开 export 直接指定。
-- 旧 Claude Code / Claude.ai / legacy normalized session 继续由 source_file 派生房间，
-- 因而不要求在此表中补造记录。
CREATE TABLE IF NOT EXISTS source_sessions (
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
CREATE INDEX IF NOT EXISTS source_sessions_parent_idx ON source_sessions(parent_session_id);

-- 原始层·正文：去重后的规范内容。一条消息一行，source_uuid 为全局去重键。
-- 内容不可变：同一 uuid 多处出现（跨会话/跨文件/跨导出）只存一份，首次写入即定。
CREATE TABLE IF NOT EXISTS messages (
  source_uuid  TEXT PRIMARY KEY,            -- Claude Code/导出原生 uuid；normalized/test 用确定性哈希
  role         TEXT NOT NULL,
  speaker      TEXT NOT NULL,
  text         TEXT NOT NULL,
  timestamp    TEXT,
  parent_uuid  TEXT,
  source       TEXT NOT NULL DEFAULT 'opus-legacy',
  provider     TEXT,
  model        TEXT,
  native_message_id        TEXT,
  native_parent_message_id TEXT,
  has_image    INTEGER NOT NULL DEFAULT 0,
  image_count  INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_source_native_idx
  ON messages(source, native_message_id)
  WHERE native_message_id IS NOT NULL;

-- 原始层·发生：每个会话内的一次出现 + 排序。同一 uuid 在 N 个会话 = N 行 turns，1 行 messages。
-- created_at 是 INGEST 时间（非对话时间）；recency 一律用 messages.timestamp。
CREATE TABLE IF NOT EXISTS turns (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  source_uuid  TEXT NOT NULL REFERENCES messages(source_uuid),
  round        INTEGER NOT NULL,            -- 会话内交换序号
  message_seq  INTEGER NOT NULL,            -- 轮内位置（user=1，assistant=2..N）
  source_file  TEXT,
  line_no      INTEGER,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, source_uuid)
);
CREATE INDEX IF NOT EXISTS turns_session_order_idx ON turns(session_id, round, message_seq, line_no);
CREATE INDEX IF NOT EXISTS turns_source_uuid_idx   ON turns(source_uuid);
CREATE INDEX IF NOT EXISTS messages_timestamp_idx  ON messages(timestamp);

-- 原始层变更水位：所有 adapter 最终都写 messages/turns；Card Gen 的白天 watcher
-- 只看 turns revision，不依赖 Claude Code、Claude.ai 或未来来源各自的文件/API 事件。
CREATE TABLE IF NOT EXISTS change_watermarks (
  name       TEXT PRIMARY KEY,
  revision   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO change_watermarks (name, revision) VALUES ('turns', 0);

CREATE TRIGGER IF NOT EXISTS turns_watermark_after_insert
AFTER INSERT ON turns
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

CREATE TRIGGER IF NOT EXISTS turns_watermark_after_update
AFTER UPDATE ON turns
WHEN OLD.session_id IS NOT NEW.session_id
  OR OLD.source_uuid IS NOT NEW.source_uuid
  OR OLD.round IS NOT NEW.round
  OR OLD.message_seq IS NOT NEW.message_seq
  OR OLD.source_file IS NOT NEW.source_file
  OR OLD.line_no IS NOT NEW.line_no
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

CREATE TRIGGER IF NOT EXISTS turns_watermark_after_delete
AFTER DELETE ON turns
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

-- 原始层·会话分叉：Claude 撤回/重发或手动 fork 时，新 session 会复制旧 transcript，
-- 共享 source_uuid。delta_start_round 标记 fork 后第一轮新增内容，供首次 eligibility 与
-- 无可复用 parent tail 时的 context fallback 使用；实际 Card Gen 重喂起点可因尾卡重叠而更早。
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

-- 派生层·正文：事件卡（只留搜索/展示必要字段）
CREATE TABLE IF NOT EXISTS cards (
  card_id    TEXT PRIMARY KEY,            -- "session前缀#序号"
  session_id TEXT NOT NULL,
  turn_start INTEGER,
  turn_end   INTEGER,
  headline   TEXT NOT NULL DEFAULT '',
  share      TEXT NOT NULL DEFAULT '',     -- 全院可见
  private    TEXT NOT NULL DEFAULT '',     -- 仅 room 可见
  timestamp  TEXT,                         -- 原始对话发生时间，唯一时间字段
  room       TEXT NOT NULL,                -- 房间名（rooms.json），隐私过滤用；写入方总是显式给值
  model      TEXT                          -- 官方 API 格式原样
);
CREATE INDEX IF NOT EXISTS cards_session_idx ON cards(session_id);
CREATE INDEX IF NOT EXISTS cards_time_idx    ON cards(timestamp);

-- 卡片标签：entity overlap + 二阶共现的源
CREATE TABLE IF NOT EXISTS card_tags (
  card_id TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  tag     TEXT NOT NULL,
  PRIMARY KEY (card_id, tag)
);
CREATE INDEX IF NOT EXISTS card_tags_tag_idx ON card_tags(tag);

-- Leiden 索引层·实体节点：card_tags 去重后的规范实体（设计见 design-leiden.html Stage 2）。
-- name_embedding 用于新 tag 的 cosine 候选匹配（在线去重），backfill 阶段可为 NULL。
CREATE TABLE IF NOT EXISTS entities (
  entity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_name TEXT NOT NULL UNIQUE,
  name_embedding BLOB,
  created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- tag → 规范实体 多对一映射（含别名）。card_tags 不变，查询/建图时 JOIN 到规范实体。
-- 去重结果可重算：清空本表 + entities，重跑 build_entities 即可。
CREATE TABLE IF NOT EXISTS tag_entity_map (
  tag       TEXT PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS tag_entity_map_entity_idx ON tag_entity_map(entity_id);

-- 原始输出 + 备份元数据（与 cards 分开存）
CREATE TABLE IF NOT EXISTS card_raw (
  card_id     TEXT PRIMARY KEY REFERENCES cards(card_id) ON DELETE CASCADE,
  raw         TEXT,
  label       TEXT,
  source_file TEXT,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 派生层·档案柜：聚类（精简，时间范围/卡数从 membership 现算）
-- v2：递归 Leiden 产出层次。parent_cluster_id 指向上层社区（顶层为 NULL），
-- level 是递归深度（0=顶层）。cluster_members 只挂叶社区。
CREATE TABLE IF NOT EXISTS clusters (
  cluster_id        TEXT PRIMARY KEY,
  summary           TEXT NOT NULL DEFAULT '',
  parent_cluster_id TEXT REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  level             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS clusters_parent_idx ON clusters(parent_cluster_id);

-- 簇成员：card↔cluster 多对多，role 区分主/次归属（支持重叠）
CREATE TABLE IF NOT EXISTS cluster_members (
  card_id    TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'primary',
  PRIMARY KEY (card_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS cluster_members_card_idx    ON cluster_members(card_id);
CREATE INDEX IF NOT EXISTS cluster_members_cluster_idx ON cluster_members(cluster_id);

-- cards 全文索引（standalone，不 content-sync；Python 侧 jieba 分词后写入）
-- card_id UNINDEXED 用于回连 cards 表；headline/share/private 存 jieba 分词后的文本。
-- audience 过滤：正式关键词检索只搜 {headline share}；private 只在同房间 --card 展示。
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
  card_id UNINDEXED,
  headline, share, private
);

-- 中期层（馆员 / Midlayer，v7 2026-07-02）：夜间树快照 + 近况总结 + constant 篮子。
-- 设计见 docs/proposal-20260702-midlayer-tree-privacy.md。四表互不牵动现有五模块。

-- 夜间树快照：每晚把当前 clusters/cluster_members 原样拷进来，供叶子层身份匹配与 diff。
-- night 为本地日期字符串（如 2026-07-03）。
CREATE TABLE IF NOT EXISTS tree_snapshots (
  night             TEXT NOT NULL,
  cluster_id        TEXT NOT NULL,
  parent_cluster_id TEXT,
  level             INTEGER NOT NULL DEFAULT 0,
  summary           TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (night, cluster_id)
);
CREATE INDEX IF NOT EXISTS tree_snapshots_night_idx  ON tree_snapshots(night);
CREATE INDEX IF NOT EXISTS tree_snapshots_parent_idx ON tree_snapshots(night, parent_cluster_id);

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

-- 近况小结：每房间每晚一份（body 存历史，房间 digest.md 另存）。
CREATE TABLE IF NOT EXISTS digests (
  night      TEXT NOT NULL,
  room       TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  model      TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (night, room)
);

-- constant 篮子：只说过一次但永久有效的事实。shared=1 全院可见，否则仅 room 可见。
CREATE TABLE IF NOT EXISTS constants (
  constant_id    TEXT PRIMARY KEY,
  room           TEXT NOT NULL,
  shared         INTEGER NOT NULL DEFAULT 0,
  content        TEXT NOT NULL,
  source_card_id TEXT,
  status         TEXT NOT NULL DEFAULT 'active',
  first_seen     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS constants_room_idx   ON constants(room, status);
CREATE INDEX IF NOT EXISTS constants_shared_idx ON constants(shared, status);

-- 卡片取用日志（v8）：--card 展开时记一笔，treesnap 聚合成取用热喂 curator。
-- ts 由写入方给 UTC ISO（与 cards.timestamp 可比）；DEFAULT 只是兜底。
CREATE TABLE IF NOT EXISTS card_access (
  card_id TEXT NOT NULL,
  viewer  TEXT NOT NULL,
  source  TEXT NOT NULL DEFAULT 'search',
  ts      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS card_access_ts_idx   ON card_access(ts);
CREATE INDEX IF NOT EXISTS card_access_card_idx ON card_access(card_id, ts);
