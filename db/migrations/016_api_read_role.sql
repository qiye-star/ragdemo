-- 来源：docs/adr/0010-internal-diagnostic-web-ui-in-p1.md；
-- docs/superpowers/plans/2026-09-22-web-diagnostic-ui.md。
--
-- app_diag：内网只读诊断接口（P1 Web UI）专用连接角色。
--
-- 为什么不能用已有的 app_read：它不是只读角色——006 迁移为了 evals 评测录入
-- 场景，授予了它对 evals.* 全部表的 INSERT/UPDATE（见 006 「评测层」一节）。
-- 诊断接口的核心承诺是「后端物理上无法写入」（ADR-0010 约束「只有 GET，
-- 没有任何写接口」），用 app_read 连接这条承诺就不成立——对登录用户单独
-- REVOKE 也没用，权限是从角色继承来的，撤不掉角色本身带来的授权。
--
-- app_diag 因此是一个只给「诊断界面需要的那个精确子集」SELECT 权限的新角色，
-- 一条 INSERT/UPDATE/DELETE 都不给：
--   * asof.* 七个视图（约束「只查 asof 视图」的机械保证：误写 core.* 直接 42501）
--   * 少数不在 core.bitemporal_registry 里、因此没有 asof 视图可查的运维/配置表
--     （quality.quality_metric/dashboard、core.parse_tier_policy/parse_retry_queue、
--     core.entity 及其别名表、core.source_registry、core.ingest_watermark）
--
-- 逐张列白名单，不用 GRANT ... ON ALL TABLES IN SCHEMA：ALL TABLES 会把后续
-- 迁移在这些 schema 下新建的表自动纳入授权范围，诊断接口的可读面就会随着
-- 业务表新增而悄悄扩大，而不是每次显式决定「这张表也该给诊断界面看」。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_diag') THEN
    CREATE ROLE app_diag NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA asof, quality TO app_diag;
GRANT USAGE ON SCHEMA core TO app_diag;

-- 七张登记在案的时点表只经 asof 视图读取；app_diag 对 core.* 基表
-- 没有任何 GRANT（既没在下面出现，也没有 ALL TABLES 授权），
-- 误查会直接 InsufficientPrivilege（42501）——这是约束 5 的机械保证。
GRANT SELECT ON ALL TABLES IN SCHEMA asof TO app_diag;

-- 运维/质量表：不是时点事实表，不在 core.bitemporal_registry 里，
-- 也就没有 asof 视图可查——「只查 asof 视图」对它们不可满足，不是被违反
-- （docs/superpowers/plans/2026-09-22-web-diagnostic-ui.md 裁决 2）。
GRANT SELECT ON quality.quality_metric, quality.dashboard TO app_diag;
GRANT SELECT ON core.parse_tier_policy, core.parse_retry_queue TO app_diag;
GRANT SELECT ON core.entity, core.entity_alias, core.source_registry,
                core.ingest_watermark TO app_diag;
