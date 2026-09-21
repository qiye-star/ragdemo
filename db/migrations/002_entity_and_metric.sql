-- 来源：docs/02-data-model.md §2、§3。改动需同步该文档。

-- §2.1 实体（非时点表：缓变描述表，存「当前」画像）
CREATE TABLE core.entity (
  entity_id       text PRIMARY KEY
                  CHECK (entity_id ~ '^[A-Z]{2}[.][A-Za-z0-9._-]{1,32}$'),
  name_full       text NOT NULL,
  name_short      text,
  name_en         text,
  entity_type     core.entity_type NOT NULL,
  market          text,                       -- SSE/SZSE/BSE/HKEX/NASDAQ/NYSE/TWSE/KRX
  tushare_code    text UNIQUE,
  ifind_code      text,
  wind_code       text,
  edgar_cik       text,
  l1_layer        text NOT NULL,              -- 算力 / 模型 / 应用 / 数据 / 能源 ...
  l2_segment      text NOT NULL,
  l3_node         text[] NOT NULL DEFAULT '{}',
  primary_node    text NOT NULL,
  ai_revenue_pct  numeric(5,4) CHECK (ai_revenue_pct BETWEEN 0 AND 1),
  ai_revenue_src  text,
  hq_country      text,
  status          core.entity_status NOT NULL DEFAULT 'active',
  listed_date     date,
  currency        char(3),                    -- 主要报告币种
  fiscal_year_end smallint CHECK (fiscal_year_end BETWEEN 1 AND 12),
  notes           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT entity_primary_node_in_l3 CHECK (primary_node = ANY (l3_node))
);

CREATE INDEX entity_primary_node_idx ON core.entity (primary_node);
CREATE INDEX entity_l3_node_gin      ON core.entity USING gin (l3_node);
CREATE INDEX entity_status_idx       ON core.entity (status) WHERE status = 'active';

-- §2.2 别名。不设全局唯一约束：「中兴」「长城」这类别名天然一对多。
CREATE TABLE core.entity_alias (
  alias       text NOT NULL,
  entity_id   text NOT NULL REFERENCES core.entity ON DELETE CASCADE,
  alias_type  text NOT NULL,     -- full / short / en / ticker / product / former / nickname
  source      text NOT NULL,
  confidence  core.confidence NOT NULL DEFAULT 'high',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (alias, entity_id)
);

CREATE INDEX entity_alias_entity_idx ON core.entity_alias (entity_id);
CREATE INDEX entity_alias_trgm       ON core.entity_alias USING gin (alias gin_trgm_ops);

-- §2.3 实体关系（时点表）
CREATE TABLE core.entity_relation (
  relation_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_entity     text NOT NULL REFERENCES core.entity,
  to_entity       text NOT NULL REFERENCES core.entity,
  relation_type   core.relation_type NOT NULL,
  strength        numeric(3,2) CHECK (strength BETWEEN 0 AND 1),
  share_estimate  numeric(5,4) CHECK (share_estimate BETWEEN 0 AND 1),
  direction_note  text,
  evidence_block  bigint[] NOT NULL DEFAULT '{}',
  evidence_type   text,           -- announcement / filing / news / expert / inferred
  valid_from      date NOT NULL,
  valid_to        date,
  known_at        timestamptz NOT NULL,
  superseded_at   timestamptz,
  source          text NOT NULL,
  source_ref      text,
  ingest_run_id   text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  confidence      core.confidence NOT NULL DEFAULT 'medium',
  CONSTRAINT relation_no_self CHECK (from_entity <> to_entity),
  CONSTRAINT relation_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE UNIQUE INDEX entity_relation_live_uk
  ON core.entity_relation (from_entity, to_entity, relation_type, valid_from)
  WHERE superseded_at IS NULL;

CREATE INDEX entity_relation_from ON core.entity_relation (from_entity, relation_type, known_at DESC);
CREATE INDEX entity_relation_to   ON core.entity_relation (to_entity,   relation_type, known_at DESC);

-- §2.4 环节归属（时点表）。观点评分的基准是「L3 环节等权组合」，
-- 环节成分只存当前值会让回溯修改静默改变历史基准收益。
CREATE TABLE core.entity_node_membership (
  membership_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     text NOT NULL REFERENCES core.entity,
  l3_node       text NOT NULL,
  is_primary    boolean NOT NULL DEFAULT false,
  valid_from    date NOT NULL,
  known_at      timestamptz NOT NULL,
  superseded_at timestamptz,
  source        text NOT NULL,          -- 通常是 'manual:<author>'
  source_ref    text,
  ingest_run_id text NOT NULL,
  ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX entity_node_membership_live_uk
  ON core.entity_node_membership (entity_id, l3_node)
  WHERE superseded_at IS NULL;

CREATE INDEX entity_node_membership_node ON core.entity_node_membership (l3_node, known_at DESC);

-- §3 指标与规则层
CREATE TABLE core.node_metric (
  metric_id       text PRIMARY KEY CHECK (metric_id ~ '^[a-z][a-z0-9_]{2,63}$'),
  l3_node         text,
  metric_name     text NOT NULL,
  metric_role     core.metric_role NOT NULL,
  frequency       text NOT NULL,        -- daily / monthly / quarterly / annual / irregular
  source_type     text NOT NULL,        -- filing / announcement / vendor_api / ir_page / derived
  source_entity   text[] NOT NULL DEFAULT '{}',
  extraction_hint text,                 -- 给抽取 Agent 的定位提示
  direction       smallint NOT NULL DEFAULT 1 CHECK (direction IN (-1, 1)),
  definition      text NOT NULL,        -- 口径定义，必填
  unit            text NOT NULL,        -- CNY / USD / pcs / pct / ratio / x
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.metric_source_map (
  metric_id      text NOT NULL REFERENCES core.node_metric,
  provider       text NOT NULL,
  provider_field text NOT NULL,
  priority       smallint NOT NULL DEFAULT 100,
  scale_factor   numeric NOT NULL DEFAULT 1,   -- 供应商单位 -> 本系统单位
  notes          text,
  PRIMARY KEY (metric_id, provider, provider_field)
);

CREATE TABLE core.ai_revenue_rule (
  rule_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      text NOT NULL REFERENCES core.entity,
  rule_text      text NOT NULL,
  source_type    text NOT NULL,     -- segment_report / management_guidance / analyst / estimate
  confidence     core.confidence NOT NULL,
  author         text NOT NULL,
  effective_from date NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- mechanism 必填是刻意的：没有机制说明的规则无法进入输出，因为输出必须能解释「为什么」。
CREATE TABLE core.propagation_rule (
  rule_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trigger_node   text NOT NULL,
  trigger_event  text NOT NULL,
  affected_node  text NOT NULL,
  direction      smallint NOT NULL CHECK (direction IN (-1, 0, 1)),
  lag_days       int NOT NULL DEFAULT 0 CHECK (lag_days >= 0),
  mechanism      text NOT NULL,     -- 机制说明，必填，进入输出的解释部分
  weight_field   text,              -- 加权依据字段名，如 'share_estimate'
  confidence     core.confidence NOT NULL,
  evidence_block bigint[] NOT NULL DEFAULT '{}',
  author         text NOT NULL,
  enabled        boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX propagation_rule_trigger
  ON core.propagation_rule (trigger_node, trigger_event) WHERE enabled;
