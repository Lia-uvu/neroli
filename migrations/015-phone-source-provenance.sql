-- v15: phone-booth journals live under enrolled Claude project directories but
-- are a distinct local source. Correct Cards classified as claude-code by v14.

UPDATE cards
SET source = 'phone'
WHERE EXISTS (
  SELECT 1 FROM turns
  WHERE turns.session_id = cards.session_id
    AND turns.source_file GLOB '*/phone-*.jsonl'
);

PRAGMA user_version = 15;
