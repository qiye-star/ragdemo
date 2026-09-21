-- 来源：docs/02-data-model.md §6–§9、docs/04-ingestion.md §4。改动需同步这两份文档。

-- §6 观点与事件层
CREATE TABLE core.event (
  event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_type        text NOT NULL,     -- capex_guidance / capacity_expansion / export_control /
                                       -- order_win / product_launch / earnings / policy ...
  trigger_entity    text REFERENCES core.entity,
  trigger_node      text,
  summary           text NOT NULL,
  publish_at        timestamptz NOT NULL,
  source_block      bigint REFERENCES core.doc_block,
  matched_rules     bigint[] NOT NULL DEFAULT '{}',
  affected_entities jsonb NOT NULL DEFAULT '[]',  -- [{entity_id, direction, weight, lag_days}]
  processed_at      timestamptz,
  valid_from        date NOT NULL,
  known_at          timestamptz NOT NULL,
  superseded_at     timestamptz,
  source            text NOT NULL,
  source_ref        text,
  ingest_run_id     text NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now(),
  -- docs/02-data-model.md §9 不变量 2 要求登记在册的七张表都有这条 CHECK。
  CONSTRAINT event_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

CREATE INDEX event_type_time   ON core.event (event_type, publish_at DESC);
CREATE INDEX event_trigger     ON core.event (trigger_entity, publish_at DESC);
CREATE INDEX event_unprocessed ON core.event (known_at) WHERE processed_at IS NULL;
CREATE INDEX event_affected    ON core.event USING gin (affected_entities);

-- opinion_has_evidence 在数据库层面强制「观点必须有依据」——合规硬约束的最后一道防线。
-- opinion 不是时点表：它本身就是一个在 as_of 时刻产生的不可变事实，只追加不修改。
CREATE TABLE core.opinion (
  opinion_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id       text REFERENCES core.entity,
  l3_node         text,
  event_id        bigint REFERENCES core.event,
  direction       core.opinion_direction NOT NULL,
  confidence      core.confidence NOT NULL,
  thesis          text NOT NULL,
  evidence_blocks bigint[] NOT NULL DEFAULT '{}',
  evidence_facts  bigint[] NOT NULL DEFAULT '{}',
  counter_thesis  text,                -- 多空辩论中对立方的最强论据，必须保留
  agent_name      text NOT NULL,
  prompt_version  text NOT NULL,
  model           text NOT NULL,
  run_id          text NOT NULL,
  as_of           timestamptz NOT NULL,   -- 生成时假设的时点，评分起点
  confirmed_by    text,                   -- 人工确认者；NULL 表示未确认，不得对外
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT opinion_has_target CHECK (entity_id IS NOT NULL OR l3_node IS NOT NULL),
  CONSTRAINT opinion_has_evidence
    CHECK (cardinality(evidence_blocks) + cardinality(evidence_facts) > 0)
);

CREATE INDEX opinion_entity  ON core.opinion (entity_id, as_of DESC);
CREATE INDEX opinion_node    ON core.opinion (l3_node, as_of DESC);
CREATE INDEX opinion_event   ON core.opinion (event_id);
CREATE INDEX opinion_pending ON core.opinion (created_at) WHERE confirmed_by IS NULL;

-- 两轨分开存、UNIQUE (opinion_id, track, horizon) 保证不合并 —— adr/0007 的结构性体现。
-- benchmark_def 完整记录当次评分用的成分股清单，即使日后环节归属变化，历史分数依然可复现。
CREATE TABLE core.opinion_score (
  score_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opinion_id   bigint NOT NULL REFERENCES core.opinion,
  track        core.score_track NOT NULL,     -- price | evidence
  horizon      core.score_horizon NOT NULL,   -- 1M | 3M
  scored_at    timestamptz NOT NULL,
  score        numeric(4,2) NOT NULL CHECK (score BETWEEN -2 AND 2),
  outcome_desc text NOT NULL,
  -- price 轨专用，全部由代码计算并留存以便复现
  stock_return     numeric,
  benchmark_return numeric,
  excess_return    numeric,
  benchmark_def    jsonb,        -- {node, constituents:[...], as_of, rule_version}
  -- evidence 轨专用
  evidence_blocks bigint[] NOT NULL DEFAULT '{}',
  scorer       core.scorer NOT NULL,
  scorer_name  text,
  UNIQUE (opinion_id, track, horizon)
);

CREATE INDEX opinion_score_lookup ON core.opinion_score (track, horizon, scored_at DESC);

-- §7 评测层
CREATE TABLE evals.eval_retrieval (
  q_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question        text NOT NULL,
  as_of           timestamptz NOT NULL,
  gold_block_ids  bigint[] NOT NULL CHECK (cardinality(gold_block_ids) > 0),
  entity_filter   text[],
  doc_type_filter text[],
  difficulty      text,        -- easy / medium / hard
  notes           text,
  author          text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evals.eval_extraction (
  case_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_id        bigint NOT NULL REFERENCES core.document,
  field         text NOT NULL,
  gold_value    text NOT NULL,
  unit          text,
  gold_block_id bigint,     -- 正确答案所在块，同时评「找对地方」与「抄对数字」
  notes         text,
  author        text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (doc_id, field)
);

CREATE TABLE evals.eval_judgement (
  case_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scenario       text NOT NULL,
  as_of          timestamptz NOT NULL,
  input_ref      jsonb NOT NULL,           -- 事件 / 实体 / 文档的引用
  gold_direction core.opinion_direction NOT NULL,
  gold_reasoning text NOT NULL,
  acceptable_alternatives text[],
  author         text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- 没有历史 run 记录就无法判断指标是升是降，CI 门禁靠它。
CREATE TABLE evals.eval_run (
  run_id     text PRIMARY KEY,
  suite      text NOT NULL,          -- retrieval / extraction / judgement
  git_sha    text NOT NULL,
  config     jsonb NOT NULL,         -- 检索参数、模型、prompt 版本
  metrics    jsonb NOT NULL,         -- {recall_at_10: 0.83, mrr: 0.71, ...}
  started_at timestamptz NOT NULL,
  ended_at   timestamptz,
  cost_cents numeric(10,4)
);

-- reason_category 让修正记录可聚合——这是发现系统性缺陷的主要手段。
CREATE TABLE evals.correction_log (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_name       text NOT NULL,
  opinion_id       bigint REFERENCES core.opinion,
  original_output  text NOT NULL,
  corrected_output text NOT NULL,
  reason           text NOT NULL,
  reason_category  text,     -- wrong_number / missing_source / wrong_entity /
                             -- compliance / logic / stale_data
  corrector        text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX correction_log_category ON evals.correction_log (reason_category, created_at DESC);

-- §8 审计层。哈希链构造与校验见 docs/09-compliance-security.md §4。
-- 本表只允许 INSERT，由迁移 006 的 REVOKE UPDATE, DELETE 强制。
CREATE TABLE audit.tool_call_log (
  call_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      text NOT NULL,
  agent_name  text NOT NULL,
  tool_name   text NOT NULL,
  params      jsonb NOT NULL,
  as_of       timestamptz,          -- 该次调用使用的时点
  result_ref  text,                 -- 结果摘要或对象存储 key
  result_hash text,
  ok          boolean NOT NULL,
  error       text,
  tokens_in   int,
  tokens_out  int,
  cost_cents  numeric(10,4),
  started_at  timestamptz NOT NULL,
  ended_at    timestamptz NOT NULL,
  prev_hash   text NOT NULL,
  hash        text NOT NULL
);

CREATE UNIQUE INDEX tool_call_log_hash_uk ON audit.tool_call_log (hash);
CREATE INDEX tool_call_log_run  ON audit.tool_call_log (run_id, started_at);
CREATE INDEX tool_call_log_cost ON audit.tool_call_log (started_at) INCLUDE (cost_cents);

-- §9 一致性强制。登记表是 Schema 不变量测试与 asof 视图的共同事实来源。
CREATE TABLE core.bitemporal_registry (
  table_name regclass PRIMARY KEY,
  note       text
);

INSERT INTO core.bitemporal_registry (table_name) VALUES
  ('core.fin_fact'), ('core.price_daily'), ('core.document'), ('core.doc_block'),
  ('core.entity_relation'), ('core.entity_node_membership'), ('core.event');

-- docs/04-ingestion.md §4：实体解析三层降级的兜底出口。
-- 模糊匹配（pg_trgm）只用于给人工提供候选，绝不自动采纳。
CREATE TABLE core.entity_resolution_queue (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_ref       text NOT NULL,
  context       jsonb NOT NULL,     -- 文档、共现实体、原文片段
  candidates    jsonb NOT NULL,     -- [{entity_id, score, reason}]
  source        text NOT NULL,
  ingest_run_id text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  resolved_to   text REFERENCES core.entity,
  resolved_by   text,
  resolved_at   timestamptz
);

CREATE INDEX entity_resolution_pending ON core.entity_resolution_queue (created_at)
  WHERE resolved_at IS NULL;
