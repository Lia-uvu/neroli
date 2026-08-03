-- v13: card_nodes records Card membership, not global node ownership.
-- Inclusive rolling boundaries and fork context may put one node in many Cards.
--
-- Apply explicitly with both daytime watchers stopped:
--   sqlite3 data/fragments.db < migrations/013-card-node-membership.sql

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

ALTER TABLE card_nodes RENAME TO card_nodes_v12;
DROP INDEX card_nodes_card_idx;

CREATE TABLE card_nodes (
  card_id  TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
  node_id  TEXT NOT NULL REFERENCES conversation_nodes(node_id),
  position INTEGER NOT NULL,
  PRIMARY KEY(card_id, node_id)
);

INSERT INTO card_nodes (card_id, node_id, position)
SELECT card_id, node_id, position FROM card_nodes_v12;

DROP TABLE card_nodes_v12;
CREATE INDEX card_nodes_card_idx ON card_nodes(card_id, position);

PRAGMA user_version = 13;

COMMIT;
