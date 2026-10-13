-- 来源：docs/10.12-plan/2026-10-13-表格处理改进计划.md T18。改动需同步 docs/02-data-model.md §4。
--
-- core.fact_table_ref：结构化事实（目前全部 source='wind'）在已解析公告表格里的单元格佐证。
-- 不是事实来源：fin_fact 一行不改，fin_fact.source_block 保持"抽取自文档"的原语义（Wind 事实不是从表格抽的）。
-- report_value 是 Python 按单元格数字 × 单位倍数换算出的元值；verdict 由数据库约束与数值关系强制一致，
-- 任何路径都不可能写入一条"差值超容差却标 match"的行。
-- known_at = GREATEST(fin_fact.known_at, doc_table.known_at)：两者都可知之后，佐证才可知。
-- 只引用公共表（写入方过滤 owner_* IS NULL），因此不需要 RLS。
-- 只有 match / mismatch 落库；ambiguous / no_row / no_period_column / no_unit / not_cny 只在校验报告里出现。

CREATE TABLE core.fact_table_ref (
  ref_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fact_id        bigint  NOT NULL REFERENCES core.fin_fact,
  table_id       bigint  NOT NULL REFERENCES core.doc_table,
  block_id       bigint  NOT NULL REFERENCES core.doc_block,
  cell_row       int     NOT NULL CHECK (cell_row >= 0),
  cell_col       int     NOT NULL CHECK (cell_col >= 0),
  page           int,
  entity_id      text    NOT NULL REFERENCES core.entity,
  metric_id      text    NOT NULL,
  period         text    NOT NULL,
  report_value   numeric NOT NULL,
  fact_value     numeric NOT NULL,
  tolerance      numeric NOT NULL CHECK (tolerance >= 0),
  unit_scale     numeric NOT NULL CHECK (unit_scale > 0),
  period_label   text    NOT NULL,
  verdict        text    NOT NULL CHECK (verdict IN ('match', 'mismatch')),
  check_version  text    NOT NULL,
  valid_from     date    NOT NULL,
  known_at       timestamptz NOT NULL,
  superseded_at  timestamptz,
  source         text    NOT NULL,
  source_ref     text,
  ingest_run_id  text    NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fact_table_ref_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at),
  CONSTRAINT fact_table_ref_verdict_is_arithmetic
    CHECK ((verdict = 'match') = (abs(report_value - fact_value) <= tolerance))
);

CREATE UNIQUE INDEX fact_table_ref_live_uk
  ON core.fact_table_ref (fact_id, block_id, cell_row, cell_col) WHERE superseded_at IS NULL;
CREATE INDEX fact_table_ref_fact     ON core.fact_table_ref (fact_id) WHERE superseded_at IS NULL;
CREATE INDEX fact_table_ref_known_at ON core.fact_table_ref (known_at);

INSERT INTO core.bitemporal_registry (table_name, note) VALUES
  ('core.fact_table_ref', '事实的表格单元格佐证（表格处理改进计划 T18）');

CREATE VIEW asof.fact_table_ref AS
SELECT * FROM core.fact_table_ref
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

ALTER TABLE core.fact_table_ref OWNER TO app_owner;
ALTER VIEW  asof.fact_table_ref OWNER TO app_owner;
GRANT INSERT ON core.fact_table_ref TO app_write;
GRANT UPDATE (superseded_at) ON core.fact_table_ref TO app_write;
GRANT SELECT ON asof.fact_table_ref TO app_read, app_diag;
