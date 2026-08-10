-- v14: persist the adapter/source that produced each derived Card.
-- Tree and normalized-v2 sessions have explicit provenance. Legacy Claude Code
-- sessions are recognized by their enrolled source-file layout; everything else
-- remains explicit `legacy` rather than guessing from speaker/model labels.

ALTER TABLE cards ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy';

UPDATE cards
SET source = (
  SELECT branch.source
  FROM conversation_card_branches branch
  WHERE branch.session_id = cards.session_id
)
WHERE EXISTS (
  SELECT 1 FROM conversation_card_branches branch
  WHERE branch.session_id = cards.session_id
);

UPDATE cards
SET source = (
  SELECT session.source
  FROM source_sessions session
  WHERE session.session_id = cards.session_id
)
WHERE source = 'legacy' AND EXISTS (
  SELECT 1 FROM source_sessions session
  WHERE session.session_id = cards.session_id
);

UPDATE cards
SET source = 'claude-code'
WHERE source = 'legacy' AND EXISTS (
  SELECT 1 FROM turns
  WHERE turns.session_id = cards.session_id
    AND turns.source_file LIKE '%/.claude/projects/%'
);

CREATE INDEX cards_source_idx ON cards(source);

PRAGMA user_version = 14;
