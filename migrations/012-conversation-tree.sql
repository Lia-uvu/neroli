-- v12: source-neutral conversation forest, rendering observations, and
-- Card-layer branch/coverage projection.
--
-- Apply explicitly with both daytime watchers stopped:
--   sqlite3 data/fragments.db < migrations/012-conversation-tree.sql

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE conversation_nodes (
  node_id               TEXT PRIMARY KEY,
  source                TEXT NOT NULL,
  room                  TEXT NOT NULL,
  native_node_id        TEXT NOT NULL,
  parent_node_id        TEXT REFERENCES conversation_nodes(node_id),
  native_parent_node_id TEXT,
  occurred_at           TEXT,
  kind                  TEXT NOT NULL CHECK(kind IN ('message', 'tool', 'checkpoint', 'event')),
  source_type           TEXT NOT NULL,
  role                  TEXT CHECK(role IS NULL OR role IN ('user', 'assistant')),
  text                  TEXT,
  message_json          TEXT,
  provider              TEXT,
  model                 TEXT,
  created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source, room, native_node_id),
  CHECK(
    (kind = 'message' AND role IS NOT NULL AND text IS NOT NULL AND message_json IS NOT NULL)
    OR
    (kind != 'message' AND role IS NULL AND text IS NULL AND message_json IS NULL)
  )
);
CREATE INDEX conversation_nodes_parent_idx ON conversation_nodes(parent_node_id);
CREATE INDEX conversation_nodes_room_time_idx
  ON conversation_nodes(room, occurred_at, node_id);

CREATE TABLE conversation_observations (
  observation_id   TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  room             TEXT NOT NULL,
  native_context_id TEXT NOT NULL,
  kind             TEXT NOT NULL,
  observed_at      TEXT NOT NULL,
  node_id          TEXT NOT NULL REFERENCES conversation_nodes(node_id),
  native_node_id   TEXT NOT NULL,
  payload_json     TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX conversation_observations_context_idx
  ON conversation_observations(source, room, native_context_id, kind, observed_at);
CREATE INDEX conversation_observations_node_idx
  ON conversation_observations(node_id);

-- Derived Card processing state. This is deliberately not canonical branch
-- status: the first discovered child keeps the existing Card stream and later
-- siblings get peer streams which reuse the shared prefix as context.
CREATE TABLE conversation_card_branches (
  session_id        TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  room              TEXT NOT NULL,
  anchor_node_id    TEXT NOT NULL UNIQUE REFERENCES conversation_nodes(node_id),
  parent_session_id TEXT REFERENCES conversation_card_branches(session_id),
  fork_node_id      TEXT REFERENCES conversation_nodes(node_id),
  created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX conversation_card_branches_parent_idx
  ON conversation_card_branches(parent_session_id);

CREATE TABLE conversation_node_branches (
  node_id    TEXT PRIMARY KEY REFERENCES conversation_nodes(node_id),
  session_id TEXT NOT NULL REFERENCES conversation_card_branches(session_id)
);
CREATE INDEX conversation_node_branches_session_idx
  ON conversation_node_branches(session_id);

ALTER TABLE turns ADD COLUMN is_context INTEGER NOT NULL DEFAULT 0;

DROP TRIGGER turns_watermark_after_update;
CREATE TRIGGER turns_watermark_after_update
AFTER UPDATE ON turns
WHEN OLD.session_id IS NOT NEW.session_id
  OR OLD.source_uuid IS NOT NEW.source_uuid
  OR OLD.round IS NOT NEW.round
  OR OLD.message_seq IS NOT NEW.message_seq
  OR OLD.source_file IS NOT NEW.source_file
  OR OLD.line_no IS NOT NEW.line_no
  OR OLD.is_context IS NOT NEW.is_context
BEGIN
  UPDATE change_watermarks
  SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
  WHERE name = 'turns';
END;

CREATE TABLE card_nodes (
  card_id  TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  node_id  TEXT NOT NULL UNIQUE REFERENCES conversation_nodes(node_id),
  position INTEGER NOT NULL,
  PRIMARY KEY(card_id, node_id)
);
CREATE INDEX card_nodes_card_idx ON card_nodes(card_id, position);

PRAGMA user_version = 12;

COMMIT;
