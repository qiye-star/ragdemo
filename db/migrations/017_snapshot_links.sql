-- 来源：docs/04-ingestion.md §2.1、CLAUDE.md §1.5「入库时同时保留原始响应」，
-- docs/9.28-plan/2026-09-28-数据底座完善总计划.md 阶段 1 任务 1.1。
--
-- 每一行事实 / 行情 / 文档都要能回到它所出自的那一次原始响应。source_ref 已经
-- 被业务键占用（fin_fact 是 "ts_code:end_date:field"，document 是供应商文档 ID），
-- 而且一次响应产出多行，所以另开一列外键，不复用 source_ref。
--
-- 允许 NULL：本迁移之前写入的存量行没有快照可指（它们的原始响应当年就没留下，
-- 这正是本迁移要堵的洞，无法事后补），种子/手工夹具也不经过供应商调用。
-- 新写入的行是否带上 snapshot_id 由写入路径的测试守着，不由 NOT NULL 守——
-- 存量行不能被一条约束追溯成违规。

ALTER TABLE core.fin_fact    ADD COLUMN snapshot_id bigint REFERENCES core.provider_snapshot;
ALTER TABLE core.price_daily ADD COLUMN snapshot_id bigint REFERENCES core.provider_snapshot;
ALTER TABLE core.document    ADD COLUMN snapshot_id bigint REFERENCES core.provider_snapshot;

CREATE INDEX fin_fact_snapshot    ON core.fin_fact (snapshot_id)    WHERE snapshot_id IS NOT NULL;
CREATE INDEX price_daily_snapshot ON core.price_daily (snapshot_id) WHERE snapshot_id IS NOT NULL;
CREATE INDEX document_snapshot    ON core.document (snapshot_id)    WHERE snapshot_id IS NOT NULL;

-- asof 视图用 SELECT * 展开时把列锁死在视图定义里，新增列必须重建视图才会出现。
-- CREATE OR REPLACE 只允许在末尾追加列，ADD COLUMN 天然满足；属主与授权保持不变。
CREATE OR REPLACE VIEW asof.fin_fact AS
SELECT * FROM core.fin_fact
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE OR REPLACE VIEW asof.price_daily AS
SELECT * FROM core.price_daily
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE OR REPLACE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());
