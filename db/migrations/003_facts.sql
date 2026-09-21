-- 来源：docs/02-data-model.md §4。改动需同步该文档。

-- §4.1 fin_fact_live_uk 是核心约束：同一 (实体, 指标, 期间) 在任一时刻只能有一行
-- 「当前有效」。更正必然表现为「给旧行打 superseded_at + 插入新行」。
CREATE TABLE core.fin_fact (
  fact_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     text NOT NULL REFERENCES core.entity,
  metric_id     text NOT NULL REFERENCES core.node_metric,
  period        text NOT NULL,            -- '2024Q3' / '2024FY' / '2024-08'
  period_end    date NOT NULL,            -- 期间结束日，用于排序与区间查询
  value         numeric NOT NULL,
  unit          text NOT NULL,
  currency      char(3),
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,
  source_ref    text,
  source_block  bigint,                   -- 若抽取自文档，指向 doc_block
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fin_fact_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE UNIQUE INDEX fin_fact_live_uk
  ON core.fin_fact (entity_id, metric_id, period)
  WHERE superseded_at IS NULL;

CREATE INDEX fin_fact_lookup   ON core.fin_fact (entity_id, metric_id, period_end DESC, known_at DESC);
CREATE INDEX fin_fact_known_at ON core.fin_fact (known_at);
CREATE INDEX fin_fact_metric   ON core.fin_fact (metric_id, period_end DESC);

-- §4.2 adj_factor 是本表需要双时间轴的主要原因：每次除权除息供应商都会回溯调整
-- 全部历史复权因子，直接 UPDATE 会让三个月前算出的观点分数今天重算就变。
CREATE TABLE core.price_daily (
  entity_id     text NOT NULL REFERENCES core.entity,
  trade_date    date NOT NULL,
  open          numeric,
  high          numeric,
  low           numeric,
  close         numeric NOT NULL,
  pre_close     numeric,
  volume        numeric,
  amount        numeric,
  adj_factor    numeric NOT NULL DEFAULT 1,
  is_suspended  boolean NOT NULL DEFAULT false,
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,
  source_ref    text,
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, trade_date, known_at)
);

CREATE UNIQUE INDEX price_daily_live_uk
  ON core.price_daily (entity_id, trade_date)
  WHERE superseded_at IS NULL;

CREATE INDEX price_daily_date ON core.price_daily (trade_date, entity_id);

-- §4.3 原始响应永不修改、永不删除。任何「数据对不对」的争议都回到这张表重放。
CREATE TABLE core.provider_snapshot (
  snapshot_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider      text NOT NULL,
  endpoint      text NOT NULL,
  params        jsonb NOT NULL,
  response      jsonb,
  response_ref  text,                -- 超过 1MB 的响应存对象存储，这里放 key
  http_status   int,
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  ingest_run_id text NOT NULL,
  cost_cents    numeric(10,4)
);

CREATE INDEX provider_snapshot_lookup ON core.provider_snapshot (provider, endpoint, fetched_at DESC);
CREATE INDEX provider_snapshot_params ON core.provider_snapshot USING gin (params);
