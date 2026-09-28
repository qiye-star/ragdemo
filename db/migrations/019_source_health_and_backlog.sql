-- 来源：docs/adr/0012-data-source-roles-and-degradation.md。改动需同步那篇 ADR。
--
-- 两张运维状态表，都不是时点事实表——不登记进 core.bitemporal_registry，
-- 不需要 known_at/superseded_at/asof 视图（与 013 的 core.ingest_watermark 同理）。

-- 各数据源层的冷却状态。跨进程生效：Dagster 每个 run 是新进程，被限频
-- 1 次/小时的接口不该每个 run 都再被拒一次。
CREATE TABLE core.source_health (
  layer                text NOT NULL,     -- 分工表里的层名，如 'tushare-mcp'
  capability           text NOT NULL,     -- 'price_daily' / 'financials' / ...
  blocked_until        timestamptz,       -- NULL 或早于现在 = 可用
  last_failure_kind    text,
  last_failure_message text,
  last_failure_at      timestamptz,
  last_success_at      timestamptz,
  PRIMARY KEY (layer, capability),
  CONSTRAINT source_health_kind CHECK (
    last_failure_kind IS NULL
    OR last_failure_kind IN ('permission', 'rate_limited', 'unavailable', 'rejected')
  )
);

COMMENT ON TABLE core.source_health IS
  '各数据源层的冷却状态与最近一次成败（ADR-0012）。运维状态，不是时点事实表。';

-- 没取到、又不允许跨源补的数据（财务：ADR-0012 规定不跨源写入）。下次触发时
-- 先补这里的未解决项；补成功才记 resolved_at。同一 (capability, entity_ref) 同时
-- 只有一条未解决项，since 取最早缺口。
CREATE TABLE core.ingest_backlog (
  backlog_id      bigserial PRIMARY KEY,
  capability      text NOT NULL,
  entity_ref      text NOT NULL,
  since           date NOT NULL,          -- 最早缺失的报告期末 / 交易日
  failure_kind    text NOT NULL,
  reason          text NOT NULL,
  attempts        integer NOT NULL DEFAULT 1,
  first_failed_at timestamptz NOT NULL DEFAULT now(),
  last_failed_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz,
  CONSTRAINT ingest_backlog_kind CHECK (
    failure_kind IN ('permission', 'rate_limited', 'unavailable', 'rejected')
  ),
  CONSTRAINT ingest_backlog_time_order CHECK (
    resolved_at IS NULL OR resolved_at >= first_failed_at
  )
);

CREATE UNIQUE INDEX ingest_backlog_open_uk
  ON core.ingest_backlog (capability, entity_ref)
  WHERE resolved_at IS NULL;

COMMENT ON TABLE core.ingest_backlog IS
  '待补清单（ADR-0012）：主源不可用且不允许降级写入的数据。运维状态，不是时点事实表。';

-- 与 009/011/013 同一句：新表不能停在超级用户名下（不变量 6）。
ALTER TABLE core.source_health OWNER TO app_owner;
ALTER TABLE core.ingest_backlog OWNER TO app_owner;
ALTER SEQUENCE core.ingest_backlog_backlog_id_seq OWNER TO app_owner;

GRANT SELECT, INSERT, UPDATE ON core.source_health TO app_write;
GRANT SELECT, INSERT, UPDATE ON core.ingest_backlog TO app_write;
GRANT USAGE ON SEQUENCE core.ingest_backlog_backlog_id_seq TO app_write;
GRANT SELECT ON core.source_health, core.ingest_backlog TO app_read;
