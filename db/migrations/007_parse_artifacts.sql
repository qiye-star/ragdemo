-- 来源：docs/05-document-pipeline.md §2、docs/adr/0008-textin-xparse-document-parsing.md。
-- 改动需同步这两份文档。
--
-- 背景：路径 B 的解析器从 MinerU 换成 TextIn xParse（ADR-0008）。
-- xParse 一次调用同时产出结构化 detail[] 与 Markdown 全文，两者都要存档：
--   - JSON 是**重切块的数据源**。改切块参数、改过滤规则时，从 JSON 重切，
--     不重新调用按页计费的 API。
--   - Markdown 是**人读的档案**，用于人工核对与跨版本 diff。
-- 两者体积都可达数十 MB，不入库、不进 jsonb 列——TOAST 之后 pg_dump 会被拖垮，
-- 而这两样东西从来不参与关系查询。存对象存储，表里只留 key，
-- 与 core.provider_snapshot.response_ref 是同一套做法（docs/02-data-model.md §4）。

ALTER TABLE core.document
  ADD COLUMN parse_json_ref  text,       -- xParse 完整响应 JSON 的对象存储 key
  ADD COLUMN parse_md_ref    text,       -- result.markdown 的对象存储 key
  ADD COLUMN parse_warnings  jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN core.document.parse_json_ref IS
  'xParse 完整响应的对象存储 key：parse/textin/<content_hash>/<param_fp>.json。'
  '重切块的数据源，命中即不重复调用计费 API（ADR-0008 后果 2）。';
COMMENT ON COLUMN core.document.parse_md_ref IS
  'result.markdown 的对象存储 key：parse/textin/<content_hash>/<param_fp>.md。'
  '存档与人工核对用，不是检索输入。';
COMMENT ON COLUMN core.document.parse_warnings IS
  '解析告警数组，如 ["page 12: table structure uncertain"]。'
  '消费方：检索降权与抽取置信度降档（docs/07-agents.md §5）。';

-- docs/05-document-pipeline.md §1：检索评测按 parse_engine 分组统计，
-- 这是判断供应商是否值钱的依据。另外 'textin:skipped' 的计数是运维指标
-- （ADR-0008 后果 6），两个用途都要扫这一列。
CREATE INDEX document_parse_engine ON core.document (parse_engine);

-- 视图必须重建。006_asof_views_and_roles.sql 已经写明这个陷阱：
-- asof.document 用 SELECT *，列清单在 CREATE VIEW 时就固定了，
-- 给基表加列不会自动出现在视图里。而应用层与 Agent 只允许查 asof 视图
-- （CLAUDE.md §1.1），不重建的话上面三列在整个应用侧永远不可见——
-- 不报错，只是查不到。
--
-- CREATE OR REPLACE VIEW 只允许在**末尾追加**列，不允许改动已有列的名字、
-- 类型或顺序。新增列由 ALTER TABLE ... ADD COLUMN 追加在表末尾，
-- 因此 SELECT * 展开后正好是「原列序 + 三个新列」，满足这条限制。
-- 若将来需要删列或改列序，就不能用 REPLACE，必须 DROP VIEW 后重建，
-- 届时要一并重新 GRANT（REPLACE 会保留授权，DROP 不会）。
CREATE OR REPLACE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
