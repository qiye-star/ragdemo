-- 来源：docs/10.12-plan/2026-10-13-表格处理改进计划.md T8。改动需同步 docs/02-data-model.md §5、docs/05-document-pipeline.md §3.2。
--
-- core.doc_table：表格块的单元格网格（引擎无关）。一行 = 一个表格 doc_block 的一版网格。
-- 表格块本身（content / content_desc / embedding）仍是检索单元，保持"不切分"；这里只是它的结构化形态，
-- 供逐格溯源 [block_id | page | r{row}c{col}]、逐格校验与事实佐证使用。
--
-- 时点：known_at / valid_from / publish_at 等全部从所属 doc_block 复制（写入方从库里读回，不接受调用方传值）；
-- ingested_at 只做运维排查，永不进入 as_of 过滤。网格换版本（grid_version / verify_version 变化）时
-- 旧行标 superseded_at、插新行，同一块任一时刻最多一行 live（doc_table_live_uk）。
-- 所属文档被重解析时，DocumentWriter 在标旧块 superseded_at 的同一事务里标旧网格。
--
-- grid 内单元格文本是原文；研报（can_show_raw = false，ADR-0014）照常存储以供校验，
-- 但任何展示路径都不得输出（tables/citation.py 的 display_text 在 can_show_raw = false 时为 NULL）。

CREATE TABLE core.doc_table (
  table_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  block_id                bigint NOT NULL REFERENCES core.doc_block,
  doc_id                  bigint NOT NULL REFERENCES core.document,
  grid                    jsonb  NOT NULL,
  grid_version            text   NOT NULL,
  n_rows                  int    NOT NULL CHECK (n_rows >= 1),
  n_cols                  int    NOT NULL CHECK (n_cols >= 1),
  header_rows             int    NOT NULL CHECK (header_rows >= 0),
  header_cols             int    NOT NULL CHECK (header_cols >= 0),
  pages                   int[]  NOT NULL CHECK (cardinality(pages) >= 1),
  caption                 text,
  unit                    text,
  unit_source             text   NOT NULL CHECK (unit_source IN ('in_table', 'preceding_block', 'none')),
  column_units            jsonb  NOT NULL DEFAULT '{}'::jsonb,
  row_units               jsonb  NOT NULL DEFAULT '{}'::jsonb,  -- T6 评审后：{行号: 行标签声明的单位}，与 column_units 同形
  currency                text,
  statement_kind          text   NOT NULL
    CHECK (statement_kind IN ('balance_sheet', 'income', 'cash_flow', 'key_figures', 'other')),
  source_engine           text   NOT NULL,
  provider_ref            text,
  verify_version          text   NOT NULL,
  verify_verdict          text   NOT NULL CHECK (verify_verdict IN (
                            'text_layer_cells_ok', 'text_layer_cells_partial', 'text_layer_cells_mismatch', 'text_layer_page_closed',
                            'dual_agree', 'scan_single_unverified', 'not_checked')),
  cells_numeric           int    NOT NULL CHECK (cells_numeric >= 0),
  cells_match             int    NOT NULL CHECK (cells_match >= 0),
  cells_mismatch          int    NOT NULL CHECK (cells_mismatch >= 0),
  cells_unverifiable      int    NOT NULL CHECK (cells_unverifiable >= 0),
  desc_model              text,
  desc_prompt_version     text,
  embed_input_version     text,
  continues_from_block_id bigint REFERENCES core.doc_block,
  entity_id               text REFERENCES core.entity,
  doc_type                text NOT NULL,
  publish_at              timestamptz NOT NULL,
  owner_tenant            text,
  owner_user              text,
  can_show_raw            boolean NOT NULL,
  valid_from              date NOT NULL,
  known_at                timestamptz NOT NULL,
  superseded_at           timestamptz,
  source                  text NOT NULL,
  source_ref              text,
  ingest_run_id           text NOT NULL,
  ingested_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT doc_table_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at),
  CONSTRAINT doc_table_cell_counts
    CHECK (cells_match + cells_mismatch + cells_unverifiable <= cells_numeric)
);

CREATE UNIQUE INDEX doc_table_live_uk ON core.doc_table (block_id) WHERE superseded_at IS NULL;
CREATE INDEX doc_table_doc         ON core.doc_table (doc_id);
CREATE INDEX doc_table_entity_kind ON core.doc_table (entity_id, statement_kind) WHERE superseded_at IS NULL;
CREATE INDEX doc_table_known_at    ON core.doc_table (known_at);

INSERT INTO core.bitemporal_registry (table_name, note) VALUES
  ('core.doc_table', '表格单元格网格；时点列继承所属 doc_block（表格处理改进计划 T8）');

CREATE VIEW asof.doc_table AS
SELECT * FROM core.doc_table
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

-- 行级隔离：与 006 的 block_visibility 逐字相同（ENABLE + FORCE + 策略授 PUBLIC，理由见 006 修正 5）。
ALTER TABLE core.doc_table ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.doc_table FORCE  ROW LEVEL SECURITY;
CREATE POLICY table_visibility ON core.doc_table FOR SELECT TO PUBLIC
USING (
      (owner_tenant IS NULL AND owner_user IS NULL)
   OR (owner_tenant = current_setting('app.tenant', true) AND owner_user IS NULL)
   OR (owner_user = current_setting('app.user', true))
);
CREATE POLICY table_insert ON core.doc_table FOR INSERT TO PUBLIC WITH CHECK (true);
CREATE POLICY table_update ON core.doc_table FOR UPDATE TO PUBLIC USING (true) WITH CHECK (true);

ALTER TABLE core.doc_table OWNER TO app_owner;
ALTER VIEW  asof.doc_table OWNER TO app_owner;
GRANT INSERT ON core.doc_table TO app_write;
GRANT UPDATE (superseded_at, embed_input_version) ON core.doc_table TO app_write;
GRANT SELECT ON asof.doc_table TO app_read, app_diag;
