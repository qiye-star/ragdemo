-- 来源：docs/04-ingestion.md §1、docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 B。
-- 改动需同步 docs/04-ingestion.md 与 docs/02-data-model.md。
--
-- 背景：签约前必须确认的四条款——能否本地缓存、能否展示原文片段、能否做向量索引、
-- 发布时间精度——此前只存在于合同 PDF 里，代码完全不知道它们的存在。这张表把它们
-- 变成代码约束：core.document.can_show_raw 的继承链是
-- source_registry → document → doc_block，第 4 层权限中间件只读块上那一列，
-- 不需要回溯合同；time_precision 决定 known_at 的保守边界（CLAUDE.md §1.1）。

CREATE TABLE core.source_registry (
  source_id           text PRIMARY KEY,       -- core.document.source 里出现的字符串
  vendor              text NOT NULL,
  layer               text NOT NULL,          -- structured/filing/industry/policy/news/user
  can_cache           boolean NOT NULL,       -- 四条款之一
  can_show_raw        boolean NOT NULL,       -- 四条款之二
  can_vectorize       boolean NOT NULL,       -- 四条款之三
  time_precision      text NOT NULL           -- 四条款之四
                       CHECK (time_precision IN ('second', 'minute', 'day')),
  contract_expire_at  date,
  rate_limit_qps      numeric,
  created_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE core.source_registry IS
  '每接入一个新来源（供应商或 Mock）都必须先在这里登记四条款，'
  'DocumentWriter 在写入前查它——查不到就拒绝写入（SourceNotRegistered），'
  '不允许静默假设任何一条为默认值。';

-- 006_asof_views_and_roles.sql 的属主转移是一次性地扫过当时已存在的
-- core/evals/audit 表——它跑过之后新建的表不会自动被这次扫描覆盖，
-- 停在超级用户名下会被 check_schema_invariants 的不变量 6 抓住（超级用户
-- 无条件绕过 RLS）。之后每张新建的 core.* 表都要在自己的迁移里补这一句。
ALTER TABLE core.source_registry OWNER TO app_owner;

-- 只登记 mock-announcements 这一个来源：它是仓库自己定义、自己控制的 Mock
-- （ragdemo.adapters.mock.announcements.MockAnnouncementProvider），不是要
-- 签约确认条款的真实供应商——四个值本就完全由我们自己决定，不存在"假设"
-- 一说。真实供应商（cninfo/tushare/textin 等）的条款需要商务确认后由运维
-- 手动登记，这里不代它们下判断（docs/superpowers/plans/2026-09-22-
-- data-foundation.md 阶段 B：不给未拍板的真实来源编造合规状态）。
--
-- 这一行同时保证了 P0 阶段就已经接进 Dagster DAG 的文档管线（P1 阶段 A，
-- 提交 4eadd1c）在这次迁移之后还能继续跑——definitions.py 的
-- document_writer_resource 固定用 source='mock-announcements'。
INSERT INTO core.source_registry
  (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)
VALUES
  ('mock-announcements', 'mock', 'filing', true, true, true, 'second');

-- §5.1 core.document 增列。can_show_raw 默认 true 是为了不破坏迁移前已入库的
-- 存量行的既有行为——它们写入时根本没有"四条款"这回事，缺省放行与它们当时
-- 实际被处理的方式一致；新写入的行由 DocumentWriter 从 registry 覆盖这个默认值。
ALTER TABLE core.document
  ADD COLUMN can_show_raw   boolean NOT NULL DEFAULT true,
  ADD COLUMN time_precision text;

COMMENT ON COLUMN core.document.can_show_raw IS
  '继承自 core.source_registry.can_show_raw（写入时刻的值，非实时关联）。'
  '第 4 层权限中间件读它决定能否展示原文片段。';
COMMENT ON COLUMN core.document.time_precision IS
  '继承自 core.source_registry.time_precision（写入时刻的值）。仅供审计与'
  '排查——known_at 已经按它做过保守边界调整，本列不参与任何查询过滤。';

-- core.doc_block 同样需要这一列：第 4 层权限中间件"只读块上这一个字段，
-- 不需要回溯合同"（数据底座方案 §2.4），JOIN 回 document 会让过滤条件
-- 无法下推到 BM25/HNSW 索引（004_documents.sql 反规范化列的同一个理由）。
ALTER TABLE core.doc_block
  ADD COLUMN can_show_raw boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN core.doc_block.can_show_raw IS
  '从所属 document 反规范化而来，写入时刻的值。查询过滤直接读这一列，'
  '不 JOIN core.document。';

-- 视图必须重建。007_parse_artifacts.sql 已经写明这个陷阱：CREATE OR REPLACE VIEW
-- 只允许在末尾追加列，SELECT * 展开后新列必须排在原列序之后——ALTER TABLE ...
-- ADD COLUMN 天然满足这一点。REPLACE（不是 DROP）保留既有 GRANT 与视图属主
-- （006_asof_views_and_roles.sql 的属主转移是 RLS 生效的前提，换成 DROP 就要
-- 重新处理属主与授权）。
CREATE OR REPLACE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE OR REPLACE VIEW asof.doc_block AS
SELECT * FROM core.doc_block
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
