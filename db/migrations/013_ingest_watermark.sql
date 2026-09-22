-- 来源：docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 F。
-- 改动需同步 docs/04-ingestion.md。
--
-- 背景：现在每次 run 都按 partition_date 全量拉——能跑，但拉取量随历史线性
-- 增长（阶段 F 引言原话）。这张表记"上次拉到哪了"（cursor，含义由各适配器
-- 自己定义：可以是供应商的分页 token，也可以是"已处理到的最大 trade_date/
-- ann_date"），FetchContext.watermark 把它带给 fetch()，让适配器有能力只拉
-- 增量。不是所有适配器都必须使用它——不使用时行为等价于"每次全量拉"，
-- 这张表存在的意义只是"提供一个可选的增量入口"，不强制迁移全部适配器。

CREATE TABLE core.ingest_watermark (
  source_id      text NOT NULL,
  partition_date date NOT NULL,
  cursor         text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, partition_date)
);

COMMENT ON TABLE core.ingest_watermark IS
  '每源每分区的增量游标。不是时点事实表——不登记进 core.bitemporal_registry，'
  '不需要 known_at/superseded_at/asof 视图（docs/02-data-model.md §9 的登记'
  '范围是"业务事实"，这张表是运维状态）。';

-- 006_asof_views_and_roles.sql 的属主转移只扫过当时已存在的表；这张表是这次
-- 迁移新建的，停在超级用户名下会被 check_schema_invariants 的不变量 6 抓住
-- （超级用户无条件绕过 RLS）——与 009/011 两次迁移补的同一句。
ALTER TABLE core.ingest_watermark OWNER TO app_owner;

GRANT SELECT, INSERT, UPDATE ON core.ingest_watermark TO app_write;
GRANT SELECT ON core.ingest_watermark TO app_read;
