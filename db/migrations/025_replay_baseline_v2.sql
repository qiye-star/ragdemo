-- 时点泄漏抽样重放的基线哈希口径变了：不再包含 superseded_at（见 quality/replay.py::_replay_hash）。
--
-- 旧基线是按"含 superseded_at"算的，块之后被合法更正（填上 superseded_at）就会与新口径永远对不上，
-- 报出假的时点不一致。基线只是监控产物（首次抽中时记下、之后比对），不是业务事实——清空后每个
-- (block_id, as_of) 下次被抽中时按新口径重新记录，之后照常比对。
-- 代价：清空到重新积累期间，对这些历史块的"就地篡改"检测有一个窗口；块本身与它的 known_at /
-- superseded_at 都还在，没有任何业务数据丢失。
TRUNCATE quality.asof_replay_baseline;
