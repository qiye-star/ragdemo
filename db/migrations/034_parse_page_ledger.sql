-- db/migrations/034_parse_page_ledger.sql
-- 迁移 034：逐页解析台账与文档验收结论（L0 + TextIn 分页接入计划 T7；改写自解析分级路由计划 T11
-- 的 028 设计与「W1 迁移 / 记账字段设计」，028 不再使用）。
--
-- core.parse_page_route：一行 = 一份文档的一页这一次的去向（L0 / L3 / skipped）、原因、P1–P6 结论、
-- 是否向 TextIn 计费。routes_ref = core.document.parse_json_ref（分页文档是旁路 JSON
-- parse/tiered/<sha256>/<指纹>.json；整份 TextIn 文档是 TextIn JSON 的 key）。
-- core.parse_doc_acceptance：每份文档一行 P8 结论（parse/acceptance.py::verdict_for_document）。
--
-- routed_at / evaluated_at 是运维时间（解析实际发生时刻），只用于费用与告警汇总，永远不进入 as_of 过滤；
-- 两张表不登记 core.bitemporal_registry（与 core.parse_tier_policy 同理，见迁移 015）。
-- tier 'L0'（本地版面模型）是休眠的未来值：L0 v2 未通过验证（G9 未过）、未启用，验证通过前不会写入任何 L0 行；
-- 当前实际写入的只有 'L3'（TextIn）与 'skipped'（减页规则，每个跳过的页都有 skip_rule 与原因，不静默丢页）。
-- 重新解析 = 新的 routes_ref（旁路 JSON 按指纹命名），页行 ON CONFLICT DO NOTHING 不覆盖；文档结论按 routes_ref upsert。
-- 月度花费的权威是 core.provider_snapshot.cost_cents（quality/budget.py）；这里的 cost_cny 只做分级汇总，
-- NULL 表示记账时单价未配置，报表按当时单价重算。

CREATE TABLE core.parse_page_route (
  route_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  routes_ref         text NOT NULL,
  content_hash       text NOT NULL,
  doc_type           text NOT NULL,
  page               int  NOT NULL CHECK (page >= 1),
  tier               text NOT NULL CHECK (tier IN ('L0', 'L3', 'skipped')),
  engine             text NOT NULL CHECK (engine <> ''),
  page_kind          text NOT NULL CHECK (page_kind <> ''),
  reasons            text[] NOT NULL DEFAULT '{}',
  skip_rule          text,
  shadow_skip_rules  text[] NOT NULL DEFAULT '{}',
  textin_billed      boolean NOT NULL DEFAULT false,
  textin_json_ref    text,
  tier_verdict       text NOT NULL CHECK (tier_verdict IN ('passed', 'failed', 'not_run')),
  p3_state           text CHECK (p3_state IN ('pass', 'fail', 'unverifiable_scan')),
  unverified_numbers int NOT NULL DEFAULT 0 CHECK (unverified_numbers >= 0),
  output_numbers     int NOT NULL DEFAULT 0 CHECK (output_numbers >= 0),
  image_ocr_numbers  int NOT NULL DEFAULT 0 CHECK (image_ocr_numbers >= 0),
  coverage_ratio     numeric CHECK (coverage_ratio IS NULL OR coverage_ratio >= 0),
  table_blocks       int NOT NULL DEFAULT 0 CHECK (table_blocks >= 0),
  figure_count       int NOT NULL DEFAULT 0 CHECK (figure_count >= 0),
  chart_regions      int NOT NULL DEFAULT 0 CHECK (chart_regions >= 0),
  acceptance_checks  jsonb NOT NULL DEFAULT '{}',
  cost_cny           numeric CHECK (cost_cny IS NULL OR cost_cny >= 0),
  ingest_run_id      text NOT NULL,
  routed_at          timestamptz NOT NULL,
  CONSTRAINT parse_page_route_once        UNIQUE (routes_ref, page),
  CONSTRAINT parse_page_route_skip_rule   CHECK ((tier = 'skipped') = (skip_rule IS NOT NULL)),
  CONSTRAINT parse_page_route_billed_l3   CHECK (NOT textin_billed OR tier = 'L3'),
  CONSTRAINT parse_page_route_json_l3     CHECK (textin_json_ref IS NULL OR tier = 'L3'),
  CONSTRAINT parse_page_route_p3          CHECK ((tier = 'skipped') = (p3_state IS NULL)),
  CONSTRAINT parse_page_route_unverified  CHECK (unverified_numbers <= output_numbers),
  -- 写成 IS NOT NULL AND = 0：CHECK 对 NULL 视为通过，只写 cost_cny = 0 会放过"L0 费用未知"
  CONSTRAINT parse_page_route_l0_free     CHECK (tier <> 'L0' OR (cost_cny IS NOT NULL AND cost_cny = 0))
);

COMMENT ON TABLE core.parse_page_route IS
  '逐页解析台账：L0 / L3 / skipped、原因、P1–P6、计费。routed_at 是运维时间，不得用于 as_of 过滤。';

CREATE INDEX parse_page_route_month ON core.parse_page_route (routed_at, tier);

CREATE TABLE core.parse_doc_acceptance (
  routes_ref         text PRIMARY KEY,
  content_hash       text NOT NULL,
  doc_type           text NOT NULL,
  state              text NOT NULL CHECK (state IN ('accepted', 'accepted_with_scan', 'partially_accepted',
                                                   'textin_unverified', 'rejected', 'empty_document')),
  reasons            text[] NOT NULL DEFAULT '{}',
  warnings           text[] NOT NULL DEFAULT '{}',
  unverified_numbers int NOT NULL CHECK (unverified_numbers >= 0),
  scan_pages         int NOT NULL CHECK (scan_pages >= 0),
  page_count         int NOT NULL CHECK (page_count >= 0),
  l0_pages           int NOT NULL CHECK (l0_pages >= 0),
  l3_pages           int NOT NULL CHECK (l3_pages >= 0),
  skipped_pages      int NOT NULL CHECK (skipped_pages >= 0),
  billed_pages       int NOT NULL CHECK (billed_pages >= 0),
  fallback_reason    text,
  acceptance_version text NOT NULL,
  ingest_run_id      text NOT NULL,
  evaluated_at       timestamptz NOT NULL,
  CONSTRAINT parse_doc_acceptance_pages CHECK (l0_pages + l3_pages + skipped_pages = page_count)
);

COMMENT ON TABLE core.parse_doc_acceptance IS
  '每份文档的逐页验收结论（P8）。evaluated_at 是运维时间，不得用于 as_of 过滤；口径变更时按 acceptance_version 重评覆盖。';

ALTER TABLE core.parse_page_route OWNER TO app_owner;
ALTER TABLE core.parse_doc_acceptance OWNER TO app_owner;
GRANT SELECT, INSERT ON core.parse_page_route TO app_write;
GRANT SELECT, INSERT, UPDATE ON core.parse_doc_acceptance TO app_write;
GRANT SELECT ON core.parse_page_route, core.parse_doc_acceptance TO app_diag;
