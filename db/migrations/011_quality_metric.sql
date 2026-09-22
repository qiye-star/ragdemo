-- 来源：docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 D。
-- 改动需同步 docs/02-data-model.md、docs/05-document-pipeline.md。
--
-- 独立 schema（而不是塞进 core）：这张表既不是时点事实，也不受 RLS 约束——
-- 它记的是"这次运行的质量指标"，不是业务数据，混进 core 会让
-- check_schema_invariants 的时点表登记逻辑（docs/02-data-model.md §9）
-- 多一个不需要的例外。

CREATE SCHEMA IF NOT EXISTS quality;

CREATE TABLE quality.quality_metric (
  metric_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  metric         text NOT NULL,          -- 指标名，如 'parse_success_rate'
  source_id      text,                   -- 按来源分组的指标非空；跨来源的为 NULL
  partition_date date NOT NULL,
  value          numeric NOT NULL,
  threshold      numeric,                -- 判定阈值；无阈值的纯观测指标可为 NULL
  passed         boolean NOT NULL,
  note           text,                   -- 差异归因、失败原因等（阶段 F 对账会用到）
  computed_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE quality.quality_metric IS
  '每次 Dagster asset check 跑完都写一行，包括通过的——只记失败看不出'
  '"从 99% 滑到 96%" 这种退化趋势，自动指标的价值在趋势不在单点。';

CREATE INDEX quality_metric_lookup ON quality.quality_metric (metric, partition_date DESC);

-- 006_asof_views_and_roles.sql 的属主转移只扫过当时已存在的 core/evals/audit
-- 表；quality 是全新 schema，不在那次扫描范围内，也不需要在——这张表不受
-- RLS 约束（check_schema_invariants 的不变量 6 只检查 core/asof），但角色
-- 授权仍然要给，否则用 app_write/app_read 连接的生产代码路径无权读写它。
GRANT USAGE ON SCHEMA quality TO app_read, app_write;
GRANT SELECT, INSERT ON quality.quality_metric TO app_write;
GRANT SELECT ON quality.quality_metric TO app_read;

-- 看板：一条 SQL 出当日全部指标的最新值。同一个 (metric, partition_date,
-- source_id) 可能因为 run 重试被写入多次，DISTINCT ON 只取每组最新的
-- computed_at——历史值仍然留在 quality_metric 里，看板只是"当前状态"的视图。
CREATE VIEW quality.dashboard AS
SELECT DISTINCT ON (metric, partition_date, source_id)
       metric, source_id, partition_date, value, threshold, passed, note, computed_at
  FROM quality.quality_metric
 ORDER BY metric, partition_date, source_id, computed_at DESC;

GRANT SELECT ON quality.dashboard TO app_read, app_write;
