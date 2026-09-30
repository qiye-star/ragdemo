-- 行业汇总类指标（全球 AI 加速卡出货量、液冷渗透率、国产化率等）：
-- 不属于任何一家公司，core.fin_fact 的 entity_id 外键放不下，单独一张时点表。
-- 来源是第三方行业报告的手工录入（ragdemo metrics import-manual），每行必须有
-- 来源与出处；known_at = 报告发布日（日精度取当日 23:59:59.999999 北京时间）。

CREATE TABLE core.industry_metric_point (
  point_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  metric_id      text NOT NULL REFERENCES core.node_metric,
  scope          text NOT NULL CHECK (scope ~ '^[A-Za-z0-9_.-]{1,32}$'),  -- GLOBAL / CN / US ...
  period         text NOT NULL,
  period_end     date NOT NULL,
  value          numeric NOT NULL,
  unit           text NOT NULL,
  valid_from     date NOT NULL,             -- = period_end
  known_at       timestamptz NOT NULL,
  superseded_at  timestamptz,
  source         text NOT NULL,             -- 报告名
  source_ref     text NOT NULL,             -- 页码 / 链接
  snapshot_id    bigint NOT NULL REFERENCES core.provider_snapshot,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT industry_metric_point_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX industry_metric_point_live_uk
  ON core.industry_metric_point (metric_id, scope, period) WHERE superseded_at IS NULL;
CREATE INDEX industry_metric_point_lookup
  ON core.industry_metric_point (metric_id, scope, period_end DESC, known_at DESC);

INSERT INTO core.bitemporal_registry (table_name) VALUES ('core.industry_metric_point');

CREATE VIEW asof.industry_metric_point AS
SELECT * FROM core.industry_metric_point
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

ALTER TABLE core.industry_metric_point OWNER TO app_owner;
ALTER VIEW  asof.industry_metric_point OWNER TO app_owner;
GRANT INSERT ON core.industry_metric_point TO app_write;
GRANT UPDATE (superseded_at) ON core.industry_metric_point TO app_write;
GRANT SELECT ON asof.industry_metric_point TO app_read, app_diag;
