-- 来源：docs/9.28-plan/2026-09-28-公告双源接入计划.md Task 7。
--
-- 港交所披露易（hkexnews.hk）与 014_edgar_source_registry.sql 登记 EDGAR 同一类
-- 判断：法定指定的官方披露平台，不是需要签约确认条款的商业供应商，四条款可以
-- 直接确定，不是要商务拍板的合规决定。can_cache/can_show_raw/can_vectorize
-- 皆为 true；time_precision 取 'minute'——`adapters/hkexnews.py::_parse_publish_at`
-- 解出的 `DATE_TIME` 字段是分钟精度的真实发布时刻（"23/09/2026 06:05"），不像
-- 巨潮那样只到日、需要"当日 23:59"的保守上界（020_cninfo_source_registry.sql）。

INSERT INTO core.source_registry
  (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)
VALUES
  ('hkexnews', 'hkexnews.hk', 'filing', true, true, true, 'minute');
