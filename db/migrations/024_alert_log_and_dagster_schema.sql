-- 阶段 4：告警出口与 Dagster 存储。
--
-- audit.alert_log：每条告警不论有没有送达都落一行（渠道故障时不能让告警本身消失），并支撑
-- "同一告警键在冷却期内不重复推送"。`delivered` 记录各渠道的送达结果。
--
-- dagster schema：Dagster 的运行 / 事件 / 调度状态存进 ParadeDB 的独立 schema（总计划 4.1），
-- 表由 dagster-postgres 自己创建，这里只保证 schema 存在。

CREATE TABLE audit.alert_log (
  alert_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raised_at  timestamptz NOT NULL DEFAULT now(),
  kind       text NOT NULL,            -- run_failed / budget / quality / reconcile / test ...
  severity   text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  alert_key  text NOT NULL,            -- 去重键：同键在冷却期内只推送一次
  title      text NOT NULL,
  detail     text NOT NULL DEFAULT '',
  delivered  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"log": "ok", "webhook": "ok" | "error: …" | "skipped: 冷却中"}
  suppressed boolean NOT NULL DEFAULT false        -- 因冷却被压制、没有向外推送
);

CREATE INDEX alert_log_key_time ON audit.alert_log (alert_key, raised_at DESC);
CREATE INDEX alert_log_time ON audit.alert_log (raised_at DESC);

ALTER TABLE audit.alert_log OWNER TO app_owner;
GRANT SELECT, INSERT ON audit.alert_log TO app_write;
GRANT SELECT ON audit.alert_log TO app_diag;

CREATE SCHEMA IF NOT EXISTS dagster;
