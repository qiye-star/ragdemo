-- 来源：docs/adr/0014-research-reports-internal-retrieval-only.md。
--
-- 券商研报（东方财富公开列表）的来源登记：可缓存、可向量化（用于检索与内部研究），
-- 但 can_show_raw = false——原文不对外展示，只返回元数据与溯源。
-- 著作权归各发布券商；正文含评级 / 目标价 / 盈利预测，不得作为我们的结论输出
-- （CLAUDE.md §0），引用时按"第三方观点"标注机构与日期。
-- time_precision = 'day'：研报只给发布日期，取当日 23:59:59.999999 北京时间做保守上界。

INSERT INTO core.source_registry
  (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)
VALUES
  ('eastmoney-research', 'eastmoney.com', 'industry', true, false, true, 'day')
ON CONFLICT (source_id) DO UPDATE SET
  can_show_raw = EXCLUDED.can_show_raw,
  can_vectorize = EXCLUDED.can_vectorize,
  time_precision = EXCLUDED.time_precision;
