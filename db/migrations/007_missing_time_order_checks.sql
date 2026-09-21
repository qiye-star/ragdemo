-- 补 docs/02-data-model.md §9 不变量 2 要求、但 §2.4 与 §4.2 的 DDL 里漏掉的两条 CHECK。
--
-- 登记在 core.bitemporal_registry 的七张表都必须有 superseded_at > known_at，
-- 实际只有五张有。ragdemo_core.db.invariants.check_schema_invariants 在 P0 首次
-- 运行时抓到了这个缺口。
--
-- 为什么是新增一条迁移而不是改 002 / 003：迁移只加不改（docs/11-sdlc.md §4.2）。
-- 纠错靠新增，这样任何已经执行过 002 / 003 的库都能靠序号继续推进，
-- 而不会撞上迁移执行器的校验和保护。
--
-- 少了这条约束的后果不是理论上的：更正流程允许把 superseded_at 写成早于
-- known_at 的时刻，那一行在任何 as_of 下都不可见——数据还在，但永远查不出来。

ALTER TABLE core.price_daily
  ADD CONSTRAINT price_daily_time_order
  CHECK (superseded_at IS NULL OR superseded_at > known_at);

ALTER TABLE core.entity_node_membership
  ADD CONSTRAINT entity_node_membership_time_order
  CHECK (superseded_at IS NULL OR superseded_at > known_at);
