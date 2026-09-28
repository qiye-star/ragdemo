-- 来源：docs/9.28-plan/2026-09-28-数据底座完善总计划.md 阶段 1 任务 1.2（口径版本化），
-- docs/02-data-model.md §3、docs/03-point-in-time.md §1.1。
--
-- 口径（指标定义、供应商字段映射与换算系数、AI 收入拆分规则）以前只有"当前值"：
-- 改一个定义就覆盖掉旧的，以更早的 as_of 回测时会用上今天才写下的新口径——一种
-- 不报错的前视偏差。这里把三者改成只追加的版本记录，登记为时点表、配 asof 视图：
-- 改口径 = 追加一个新版本并把旧版本标记为 superseded，旧版本永远保留。
--
-- core.node_metric / core.metric_source_map 保留为"当前值"表：fin_fact.metric_id 与
-- metric_source_map.metric_id 都外键引用 node_metric(metric_id)，它必须一个指标一行。
-- 写入口（ragdemo/metrics/definitions.py）在同一事务里追加版本并同步当前值。
--
-- 存量行的首个版本 known_at 取固定基线 2000-01-01 00:00+08（与代码常量
-- METRIC_BASELINE_KNOWN_AT 一致，有测试守着）：它们是全部历史数据入库时所用的口径，
-- 必须在任何 as_of 下都可见，否则回测查不到这些事实对应的定义。这与种子实体
-- known_at 取 valid_from 是同一个有意接受的乐观偏差（docs/03-point-in-time.md §6.3）。
-- 之后的每一次修改，known_at 取修改真正写入的时刻。

-- --- 指标定义版本 ----------------------------------------------------------------

CREATE TABLE core.node_metric_version (
  metric_id       text NOT NULL REFERENCES core.node_metric,
  version         int  NOT NULL CHECK (version >= 1),
  l3_node         text,
  metric_name     text NOT NULL,
  metric_role     core.metric_role NOT NULL,
  frequency       text NOT NULL,
  source_type     text NOT NULL,
  source_entity   text[] NOT NULL DEFAULT '{}',
  extraction_hint text,
  direction       smallint NOT NULL DEFAULT 1 CHECK (direction IN (-1, 1)),
  definition      text NOT NULL,
  unit            text NOT NULL,
  author          text NOT NULL,
  change_note     text,
  valid_from      date NOT NULL,
  known_at        timestamptz NOT NULL,
  superseded_at   timestamptz,
  source          text NOT NULL,
  source_ref      text,
  ingest_run_id   text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric_id, version),
  CONSTRAINT node_metric_version_time_order
    CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX node_metric_version_live_uk
  ON core.node_metric_version (metric_id) WHERE superseded_at IS NULL;

INSERT INTO core.node_metric_version
  (metric_id, version, l3_node, metric_name, metric_role, frequency, source_type,
   source_entity, extraction_hint, direction, definition, unit, author, change_note,
   valid_from, known_at, source, ingest_run_id)
SELECT metric_id, 1, l3_node, metric_name, metric_role, frequency, source_type,
       source_entity, extraction_hint, direction, definition, unit, 'seed',
       '初始版本（迁移 018 由 core.node_metric 现值生成）',
       DATE '2000-01-01', TIMESTAMPTZ '2000-01-01 00:00:00+08', 'manual:seed', 'migration:018'
  FROM core.node_metric;

-- --- 供应商字段映射版本 ----------------------------------------------------------

CREATE TABLE core.metric_source_map_version (
  metric_id      text NOT NULL REFERENCES core.node_metric,
  provider       text NOT NULL,
  provider_field text NOT NULL,
  version        int  NOT NULL CHECK (version >= 1),
  priority       smallint NOT NULL DEFAULT 100,
  scale_factor   numeric NOT NULL DEFAULT 1,
  notes          text,
  author         text NOT NULL,
  change_note    text,
  valid_from     date NOT NULL,
  known_at       timestamptz NOT NULL,
  superseded_at  timestamptz,
  source         text NOT NULL,
  source_ref     text,
  ingest_run_id  text NOT NULL,
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric_id, provider, provider_field, version),
  CONSTRAINT metric_source_map_version_time_order
    CHECK (superseded_at IS NULL OR superseded_at > known_at)
);
CREATE UNIQUE INDEX metric_source_map_version_live_uk
  ON core.metric_source_map_version (metric_id, provider, provider_field)
  WHERE superseded_at IS NULL;

INSERT INTO core.metric_source_map_version
  (metric_id, provider, provider_field, version, priority, scale_factor, notes, author,
   change_note, valid_from, known_at, source, ingest_run_id)
SELECT metric_id, provider, provider_field, 1, priority, scale_factor, notes, 'seed',
       '初始版本（迁移 018 由 core.metric_source_map 现值生成）',
       DATE '2000-01-01', TIMESTAMPTZ '2000-01-01 00:00:00+08', 'manual:seed', 'migration:018'
  FROM core.metric_source_map;

-- --- AI 收入拆分规则：本身就是一条条规则记录，直接补上时点列 ----------------------
-- 没有任何表外键引用它，不需要"当前值表 + 版本表"两层；每个实体同一时刻只有一条
-- 活规则，改规则 = 追加新行 + 旧行 superseded。存量行（若有）的 known_at 取
-- created_at：规则是我们自己写下的研究判断，写下那一刻才第一次可知。

ALTER TABLE core.ai_revenue_rule
  ADD COLUMN valid_from    date,
  ADD COLUMN known_at      timestamptz,
  ADD COLUMN superseded_at timestamptz,
  ADD COLUMN source        text,
  ADD COLUMN source_ref    text,
  ADD COLUMN ingest_run_id text,
  ADD COLUMN ingested_at   timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN change_note   text;

UPDATE core.ai_revenue_rule
   SET valid_from = effective_from,
       known_at = created_at,
       source = 'manual:' || author,
       ingest_run_id = 'migration:018';

ALTER TABLE core.ai_revenue_rule
  ALTER COLUMN valid_from    SET NOT NULL,
  ALTER COLUMN known_at      SET NOT NULL,
  ALTER COLUMN source        SET NOT NULL,
  ALTER COLUMN ingest_run_id SET NOT NULL,
  ADD CONSTRAINT ai_revenue_rule_time_order
    CHECK (superseded_at IS NULL OR superseded_at > known_at);
CREATE UNIQUE INDEX ai_revenue_rule_live_uk
  ON core.ai_revenue_rule (entity_id) WHERE superseded_at IS NULL;

-- --- 登记为时点表 + asof 视图 ----------------------------------------------------
-- check_schema_invariants 要求登记表 core.X 必有同名视图 asof.X、带齐时点列，
-- 且 app_read 对 core.X 无 SELECT（只能经视图读）。

INSERT INTO core.bitemporal_registry (table_name) VALUES
  ('core.node_metric_version'),
  ('core.metric_source_map_version'),
  ('core.ai_revenue_rule');

CREATE VIEW asof.node_metric_version AS
SELECT * FROM core.node_metric_version
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.metric_source_map_version AS
SELECT * FROM core.metric_source_map_version
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.ai_revenue_rule AS
SELECT * FROM core.ai_revenue_rule
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

-- 属主必须是 app_owner，不能是运行迁移的超级用户（006 的第 6 条不变量）。
ALTER TABLE core.node_metric_version       OWNER TO app_owner;
ALTER TABLE core.metric_source_map_version OWNER TO app_owner;
ALTER VIEW  asof.node_metric_version       OWNER TO app_owner;
ALTER VIEW  asof.metric_source_map_version OWNER TO app_owner;
ALTER VIEW  asof.ai_revenue_rule           OWNER TO app_owner;

-- 006 把 core.ai_revenue_rule 当普通参考表授给了 app_read；它现在是登记在册的时点表，
-- 只能经 asof.ai_revenue_rule 读（不变量 5）。全仓库没有代码直读这张基表。
REVOKE SELECT ON core.ai_revenue_rule FROM app_read;

GRANT INSERT ON core.node_metric_version, core.metric_source_map_version TO app_write;
GRANT UPDATE (superseded_at) ON core.node_metric_version, core.metric_source_map_version,
                                core.ai_revenue_rule TO app_write;
GRANT SELECT ON asof.node_metric_version, asof.metric_source_map_version,
                asof.ai_revenue_rule TO app_read, app_diag;
