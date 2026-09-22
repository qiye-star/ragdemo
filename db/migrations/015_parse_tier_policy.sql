-- 来源：docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 G。
-- 改动需同步 docs/05-document-pipeline.md。
--
-- core.parse_tier_policy：C 档（换参数/换供应商重解析）的触发规则。带
-- known_at/superseded_at 是为了"上个月为什么触发了这么多 C 档"能被回答
-- （方案原文），但这张表记的是运维策略，不是业务事实——不登记进
-- core.bitemporal_registry：那张登记表要求的完整时点列集合
-- （valid_from/source/source_ref/ingest_run_id/ingested_at，见
-- ragdemo_core/db/invariants.py 的 BITEMPORAL_COLUMNS）本来就不适用于一张
-- 策略配置表，登记了反而会被 check_schema_invariants 的列检查判定"缺列"。

CREATE TABLE core.parse_tier_policy (
  policy_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_type         text,           -- NULL = 任意 doc_type 都适用（通配）
  confidence_below numeric,        -- NULL = 不按置信度触发
  closure_below    numeric,        -- NULL = 不按表格闭合率触发
  monthly_cap_cny  numeric NOT NULL,
  enabled          boolean NOT NULL DEFAULT true,
  known_at         timestamptz NOT NULL,
  superseded_at    timestamptz,
  CONSTRAINT parse_tier_policy_time_order CHECK (superseded_at IS NULL OR superseded_at > known_at)
);

COMMENT ON TABLE core.parse_tier_policy IS
  'C 档触发规则与月度预算上限，可配置且可审计——不写死在代码里。'
  '一个 doc_type 可能同时匹配一条精确策略与一条通配策略（doc_type IS NULL），'
  'parse/router.py::load_active_policy 优先取精确匹配的那条。';

CREATE INDEX parse_tier_policy_lookup ON core.parse_tier_policy (doc_type, known_at DESC);

ALTER TABLE core.parse_tier_policy OWNER TO app_owner;
GRANT SELECT, INSERT, UPDATE ON core.parse_tier_policy TO app_write;
GRANT SELECT ON core.parse_tier_policy TO app_read;

-- 占位默认策略：doc_type 通配、按方案 §2.2 的置信度阈值触发、月度上限给一个
-- 保守的占位数字（500 元，与 PageBudget 的默认 500 页同一个"先给个能跑的
-- 默认值，真实数字由运维按合同定价覆盖"的处理方式）——真实的 TextIn C 档
-- 单价与月度预算是商务决定，不该由代码编造；这一行存在的意义只是让路由
-- 器开箱有一条策略可用，不是这个数字本身有权威性。运维覆盖时插入新行、
-- 给旧行标 superseded_at，不原地改这一行。
INSERT INTO core.parse_tier_policy
  (doc_type, confidence_below, closure_below, monthly_cap_cny, enabled, known_at)
VALUES
  (NULL, 0.7, 0.9, 500, true, now());

-- core.parse_retry_queue：G6——解析失败（或因预算耗尽而这次没处理到）的
-- 文档必须 100% 可查，且带 24 小时超时标记。不能靠 core.document 本身记：
-- ParseRetryable 分支故意不写行（见 assets_docs.py::prepare_documents 的
-- 注释），这样下次分区重跑才会把它当"还没处理过"重新尝试——如果为了
-- "队列可见"而写一行 core.document，content_hash 去重会让下次重跑把它当
-- "已存在，跳过"，重试反而永远失效。这张表因此完全独立于 core.document，
-- 只负责"这份文档已经连续多少次没能进库"这一件事。
CREATE TABLE core.parse_retry_queue (
  source            text NOT NULL,
  provider_doc_id   text NOT NULL,
  first_failed_at   timestamptz NOT NULL,   -- 首次失败时刻，此后永久不变
  retry_deadline    timestamptz NOT NULL,   -- first_failed_at + 24 小时，同样不变
  last_seen_at      timestamptz NOT NULL,   -- 最近一次仍然失败的时刻
  attempts          int NOT NULL DEFAULT 1,
  PRIMARY KEY (source, provider_doc_id)
);

COMMENT ON TABLE core.parse_retry_queue IS
  '首次失败即入队，之后每次仍失败只推进 last_seen_at / attempts——'
  'first_failed_at 与 retry_deadline 在首次入队时就固定，与阶段 E 抽样重放'
  '"探测点只在首次抽中时固定"是同一个原则：本该在首次失败时锁定的时间戳，'
  '如果每次都重算，就再也说不清"这份文档到底卡了多久"。';

ALTER TABLE core.parse_retry_queue OWNER TO app_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON core.parse_retry_queue TO app_write;
GRANT SELECT ON core.parse_retry_queue TO app_read;
