-- 来源：docs/03-point-in-time.md §5，docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 E。
--
-- 一份公告不可能在发布之前就被知晓——`known_at >= publish_at` 是 CLAUDE.md §1.1
-- 最高优先级约束（时点一致）里最基本的一条，此前既没有 CHECK 约束也没有
-- 自检查询。加迁移前先手工核实过存量数据：
--   SELECT count(*) FROM core.document  WHERE known_at < publish_at;  -- = 0
--   SELECT count(*) FROM core.doc_block WHERE known_at < publish_at;  -- = 0
-- 两边都是 0 才能安全加约束——如果不是，得先归因、修数据，不能让迁移静默
-- 拒绝存量行或者带着违规数据硬加约束失败。

ALTER TABLE core.document
  ADD CONSTRAINT document_known_at_not_before_publish CHECK (known_at >= publish_at);

ALTER TABLE core.doc_block
  ADD CONSTRAINT doc_block_known_at_not_before_publish CHECK (known_at >= publish_at);

-- 抽样重放的哈希基线。每行记一次"以某个历史 as_of 重放查询得到的结果集
-- 哈希"，第二天再抽中同一个 block_id 时，若在同一个 as_of 下算出的哈希
-- 变了，说明这条记录在两次抽样之间被无痕修改过（known_at 被篡改、或者
-- 一条本该保持不变的历史行被更新）——这是 CLAUDE.md「回测和复现不作弊」
-- 唯一能自动化验证的手段（数据底座方案 §2.5）。
CREATE TABLE quality.asof_replay_baseline (
  block_id     bigint NOT NULL,
  as_of        timestamptz NOT NULL,
  result_hash  text NOT NULL,
  sampled_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (block_id, as_of)
);

COMMENT ON TABLE quality.asof_replay_baseline IS
  '每行 = 某个 block_id 在某个历史 as_of 下第一次被抽中时的重放结果哈希。'
  '之后同一个 (block_id, as_of) 组合再被抽中，必须算出同一个哈希，否则说明'
  '这条历史记录被无痕修改过。';

GRANT SELECT, INSERT ON quality.asof_replay_baseline TO app_write;
GRANT SELECT ON quality.asof_replay_baseline TO app_read;
