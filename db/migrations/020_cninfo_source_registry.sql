-- 来源：docs/adr/0013-cninfo-primary-announcement-source.md。
--
-- `cninfo` 这一行此前是手工 INSERT 进库的（配合海光那批公告的人工导入），
-- 不在任何迁移脚本里，干净 clone 上不存在——这本身就是一个要修的缺口。
-- 更重要的是它当初登记的 time_precision = 'second' 是错的，从没有用真实
-- 接口验证过：2026-09-28 实测 `hisAnnouncement/query` 的 announcementTime
-- 字段，取到的是北京时间当日 00:00:00.000 的整点毫秒时间戳（该接口的
-- "发布时间"只精确到日，用 0 点占位，不是真实发布时刻）。
--
-- `ingest/documents.py::DocumentWriter._known_at()` 对 time_precision='second'
-- 的来源直接信任 publish_at 的时分秒——如果适配器把这个 0 点时间戳原样
-- 传给 publish_at，known_at 就会被算成当天 0 点，比真实发布时刻提前最多
-- 一整天，是 docs/03-point-in-time.md 明确警告的前视偏差方向。
--
-- 改成 'day' 后，_known_at() 会改用当日 23:59:59.999999（北京时间）做保守
-- 上界，是既有代码路径（同一函数已经在为 local_manifest.py 那条手工导入
-- 路径做这件事，这里只是让走真实接口的路径也吃到同一条防线）。
--
-- can_cache / can_show_raw / can_vectorize 维持 true 不变：巨潮资讯网是
-- 沪深交易所指定的官方信息披露平台（不是需要签约确认条款的商业供应商，
-- 与 014_edgar_source_registry.sql 登记 EDGAR 时的推理一致），这三项本就
-- 是可以直接确定的事实。

INSERT INTO core.source_registry
  (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)
VALUES
  ('cninfo', 'cninfo.com.cn', 'filing', true, true, true, 'day')
ON CONFLICT (source_id) DO UPDATE SET
  time_precision = EXCLUDED.time_precision,
  vendor = EXCLUDED.vendor;
