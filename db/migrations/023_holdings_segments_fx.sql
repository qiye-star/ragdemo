-- 阶段二（docs/9.28-plan/2026-09-29-阶段二-股东分部汇率接入计划.md）：前十大股东、
-- 主营构成（分部）、汇率三张时点事实表。
--
-- known_at 规则：
--   * 股东 / 分部：Wind 只给"当前值"、不给披露日，known_at 取该报告期定期报告在
--     core.announcement_index 里的**最早**披露时刻（负责人 2026-09-29 决定：多份/
--     更正报告时取第一次披露日）。此后发现值变了，按"观察到的时刻"追加更正
--     （known_at_basis = 'revision_observed'），不改写历史。
--   * 汇率：CFETS 人民币汇率中间价，当日 09:15（北京时间）发布。

CREATE TABLE core.shareholder_holding (
  holding_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      text NOT NULL REFERENCES core.entity,
  period         text NOT NULL,             -- '2024FY' / '2025H1' / '2025Q1' / '2025Q3'
  period_end     date NOT NULL,
  rank           int  NOT NULL CHECK (rank BETWEEN 1 AND 10),
  holder_name    text NOT NULL,
  shares         numeric NOT NULL CHECK (shares > 0),   -- 股
  ratio          numeric NOT NULL CHECK (ratio > 0 AND ratio <= 1),  -- 0~1 小数，不是百分数
  valid_from     date NOT NULL,             -- = period_end
  known_at       timestamptz NOT NULL,
  superseded_at  timestamptz,
  known_at_basis text NOT NULL CHECK (known_at_basis IN ('first_periodic_report', 'revision_observed')),
  source         text NOT NULL,
  source_ref     text,                      -- 匹配到的公告 announcement_index.index_id
  snapshot_id    bigint NOT NULL REFERENCES core.provider_snapshot,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT shareholder_holding_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX shareholder_holding_live_uk
  ON core.shareholder_holding (entity_id, period, rank) WHERE superseded_at IS NULL;
CREATE INDEX shareholder_holding_lookup
  ON core.shareholder_holding (entity_id, period_end DESC, known_at DESC);

CREATE TABLE core.segment_revenue (
  segment_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      text NOT NULL REFERENCES core.entity,
  period         text NOT NULL,
  period_end     date NOT NULL,
  dimension      text NOT NULL CHECK (dimension IN ('product', 'region', 'industry')),
  segment_name   text NOT NULL,
  revenue        numeric NOT NULL,          -- 元
  gross_profit   numeric,                   -- 元，可空
  revenue_share  numeric,                   -- 0~1，可空
  currency       char(3) NOT NULL,
  valid_from     date NOT NULL,
  known_at       timestamptz NOT NULL,
  superseded_at  timestamptz,
  known_at_basis text NOT NULL CHECK (known_at_basis IN ('first_periodic_report', 'revision_observed')),
  source         text NOT NULL,
  source_ref     text,
  snapshot_id    bigint NOT NULL REFERENCES core.provider_snapshot,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT segment_revenue_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX segment_revenue_live_uk
  ON core.segment_revenue (entity_id, period, dimension, segment_name) WHERE superseded_at IS NULL;
CREATE INDEX segment_revenue_lookup
  ON core.segment_revenue (entity_id, period_end DESC, known_at DESC);

CREATE TABLE core.fx_rate (
  fx_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  base_ccy       char(3) NOT NULL,          -- 1 单位 base = rate 单位 quote
  quote_ccy      char(3) NOT NULL,
  rate_date      date NOT NULL,
  rate           numeric NOT NULL CHECK (rate > 0),
  valid_from     date NOT NULL,             -- = rate_date
  known_at       timestamptz NOT NULL,      -- rate_date 09:15 +08
  superseded_at  timestamptz,
  source         text NOT NULL,
  source_ref     text,
  snapshot_id    bigint NOT NULL REFERENCES core.provider_snapshot,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fx_rate_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX fx_rate_live_uk
  ON core.fx_rate (base_ccy, quote_ccy, rate_date) WHERE superseded_at IS NULL;
CREATE INDEX fx_rate_lookup ON core.fx_rate (base_ccy, quote_ccy, rate_date DESC, known_at DESC);

INSERT INTO core.bitemporal_registry (table_name) VALUES
  ('core.shareholder_holding'), ('core.segment_revenue'), ('core.fx_rate');

CREATE VIEW asof.shareholder_holding AS
SELECT * FROM core.shareholder_holding
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.segment_revenue AS
SELECT * FROM core.segment_revenue
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.fx_rate AS
SELECT * FROM core.fx_rate
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

-- 属主必须是 app_owner，不能是运行迁移的超级用户（006 的不变量 6）。
ALTER TABLE core.shareholder_holding OWNER TO app_owner;
ALTER TABLE core.segment_revenue     OWNER TO app_owner;
ALTER TABLE core.fx_rate             OWNER TO app_owner;
ALTER VIEW  asof.shareholder_holding OWNER TO app_owner;
ALTER VIEW  asof.segment_revenue     OWNER TO app_owner;
ALTER VIEW  asof.fx_rate             OWNER TO app_owner;

GRANT INSERT ON core.shareholder_holding, core.segment_revenue, core.fx_rate TO app_write;
GRANT UPDATE (superseded_at) ON core.shareholder_holding, core.segment_revenue, core.fx_rate
  TO app_write;
GRANT SELECT ON asof.shareholder_holding, asof.segment_revenue, asof.fx_rate
  TO app_read, app_diag;
