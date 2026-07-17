-- v9：事件卡字段 theme 更名为 headline（cards 主表 + FTS5 索引）。
--
-- cards_fts 是 standalone FTS5 表，不能用 ALTER COLUMN；先把已经过 jieba
-- 分词的索引内容暂存，重建虚表后原样写回，避免重新分词或丢失索引数据。
--
-- 应用：sqlite3 data/fragments.db < migrations/009-card-headline.sql

PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

CREATE TEMP TABLE cards_fts_v9 AS
SELECT card_id, theme AS headline, share, private
FROM cards_fts;

DROP TABLE cards_fts;

ALTER TABLE cards RENAME COLUMN theme TO headline;

-- parsed_json 是可查询的派生审计数据，也跟随字段契约更名；raw_output 与
-- card_raw.raw 是模型原始输出，必须保持原样，不做历史改写。
UPDATE model_calls
SET parsed_json = replace(parsed_json, '"theme":', '"headline":')
WHERE parsed_json LIKE '%"theme":%';

CREATE VIRTUAL TABLE cards_fts USING fts5(
  card_id UNINDEXED,
  headline, share, private
);

INSERT INTO cards_fts (card_id, headline, share, private)
SELECT card_id, headline, share, private
FROM cards_fts_v9;

DROP TABLE cards_fts_v9;

PRAGMA user_version = 9;

COMMIT;

PRAGMA foreign_keys = ON;
