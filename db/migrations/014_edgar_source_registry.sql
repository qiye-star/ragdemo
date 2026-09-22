-- 来源：docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 F（F5）。
-- 改动需同步 docs/04-ingestion.md。
--
-- 009_source_registry.sql 只登记了 mock-announcements（仓库自己的 Mock），
-- 明确把真实供应商的登记留给"商务确认合同条款后由运维手动登记"——理由是
-- 四条款（能否缓存/展示原文/向量化/发布时间精度）对一个真实供应商而言是
-- 需要签约确认的合规判断，代码不该替业务方拍板。
--
-- EDGAR 不属于这个待确认的范围：它是美国联邦政府记录（17 U.S.C. § 105），
-- 法律上明确处于公有领域，没有"供应商合同"这回事可谈，也没有第三方能对
-- 缓存/展示/向量化设限。四个值因此是可以直接确定的事实，不是要商务拍板的
-- 合规决定：can_cache/can_show_raw/can_vectorize 皆为 true；time_precision
-- 取 'second'——EdgarAdapter.known_at() 早就用 acceptanceDateTime（精确到秒）
-- 而不是 filingDate，登记为 'second' 只是把这个既有事实写进 registry，
-- 不改变任何一处 known_at 的实际计算方式。

INSERT INTO core.source_registry
  (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)
VALUES
  ('edgar', 'sec.gov', 'filing', true, true, true, 'second');
