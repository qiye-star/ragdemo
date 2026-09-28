-- 来源：docs/9.28-plan/2026-09-28-公告双源接入计划.md Task 2。
--
-- 公告目录：每份公告一行、不论来源、不论市场（A 股/港股/美股）。跨源去重与"降级层被
-- 主源取代"由上层的 IndexWriter（ingest/announcement_index.py）负责，这里只管表结构
-- 本身的约束。与公告全文 core.document 分开建模——目录条目不一定有全文（Wind/iFind
-- 降级层、以及暂不下载的实体都只建目录），有全文时 doc_id 指过去。

CREATE TABLE core.announcement_index (
  index_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      text NOT NULL REFERENCES core.entity,
  source         text NOT NULL CHECK (
    source IN ('cninfo', 'wind', 'ifind', 'hkexnews', 'edgar', 'edinet', 'opendart')
  ),
  source_ref     text NOT NULL,        -- 各源自己的公告/申报 ID
  title          text NOT NULL,
  title_norm     text NOT NULL,        -- 去掉"简称："前缀、NFKC、去空白，跨源比对用
  publish_date   date NOT NULL,
  url            text,
  excerpt        text,                 -- 降级层的摘录/片段；有全文的源为空
  has_full_text  boolean NOT NULL,     -- 来源本次是否给了全文（即便该来源理论上能给）
  doc_id         bigint REFERENCES core.document,
  valid_from     date NOT NULL,        -- = publish_date
  known_at       timestamptz NOT NULL, -- 各来源按自己的时间精度算好写入，这张表不重算
  superseded_at  timestamptz,
  source_note    text,                 -- 被取代时写"由 cninfo:<id> 取代"
  snapshot_id    bigint REFERENCES core.provider_snapshot,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT announcement_index_time_order
    CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

-- 现行行里 (source, source_ref) 唯一——同一份公告的同一次抓取只对应一行；重复抓取
-- 是幂等的（上层看到已有现行行就跳过，不会走到这条约束）。取代时先 UPDATE
-- superseded_at 再 INSERT 新行，旧行不占这条部分唯一索引的名额。
CREATE UNIQUE INDEX announcement_index_live_uk
  ON core.announcement_index (source, source_ref) WHERE superseded_at IS NULL;

CREATE INDEX announcement_index_entity
  ON core.announcement_index (entity_id, publish_date DESC, known_at DESC);

-- 跨源匹配用：同一实体、同一天、标题归一化后相同，判定为同一份公告（IndexWriter 用它
-- 找"这份降级行是否已经有主源在写"）。
CREATE INDEX announcement_index_match
  ON core.announcement_index (entity_id, publish_date, title_norm) WHERE superseded_at IS NULL;

-- 按 docs/02-data-model.md §9 的登记范围：这是业务事实（公告目录），登记为时点表。
INSERT INTO core.bitemporal_registry (table_name) VALUES ('core.announcement_index');

CREATE VIEW asof.announcement_index AS
SELECT * FROM core.announcement_index
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

-- 属主必须是 app_owner，不能是运行迁移的超级用户（006 的不变量 6）。
ALTER TABLE core.announcement_index OWNER TO app_owner;
ALTER VIEW  asof.announcement_index OWNER TO app_owner;

GRANT INSERT ON core.announcement_index TO app_write;
GRANT UPDATE (superseded_at, source_note, doc_id) ON core.announcement_index TO app_write;
GRANT SELECT ON asof.announcement_index TO app_read, app_diag;
