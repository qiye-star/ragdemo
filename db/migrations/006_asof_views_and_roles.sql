-- 来源：docs/03-point-in-time.md §4、docs/09-compliance-security.md §3.2。
-- 改动需同步这两份文档。
--
-- 这是 P0 唯一一条把「所有读取都带 as_of」从约定变成「数据库层面做不到违反」的迁移。
-- 实施时对文档做了五处修正，每处都在下面就近注明，文档已同步回写。

-- §4.1 取当前会话的 as_of；未设置则报错而不是回退到 now()。
-- 三处细节缺一不可：
--   1. current_setting 的第二个参数 true（missing_ok）——否则抛的是通用的
--      undefined_object，自定义提示根本不会执行；
--   2. 同时判 NULL 与空串——set_config(..., '', true) 得到的是空串；
--   3. ERRCODE = invalid_parameter_value——P0 验收测试断言的就是这个错误码。
CREATE OR REPLACE FUNCTION asof.current_as_of() RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
DECLARE v text;
BEGIN
  v := current_setting('app.as_of', true);
  IF v IS NULL OR v = '' THEN
    RAISE EXCEPTION 'app.as_of is not set; every read must declare a point in time'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  RETURN v::timestamptz;
END $$;

-- §4.1 七张时点表对应七个视图，与 core.bitemporal_registry 的登记一一对应，
-- 由 Schema 不变量测试强制（docs/02-data-model.md §9 第 3 条）。
--
-- 视图用 SELECT *，列清单在创建时就固定了。日后给基表加列（例如
-- docs/05-document-pipeline.md §7.3 的 embedding_v2）必须新增一条迁移重建视图，
-- 否则新列在 asof 侧永远看不见。
--
-- 视图**刻意不加** security_invoker：加了就要求 app_read 对 core 基表有 SELECT，
-- 与 docs/02-data-model.md §9 不变量 5 直接冲突。行级隔离改由基表上的
-- FORCE ROW LEVEL SECURITY 保证（见文件末尾）。
CREATE VIEW asof.fin_fact AS
SELECT * FROM core.fin_fact
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.doc_block AS
SELECT * FROM core.doc_block
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.price_daily AS
SELECT * FROM core.price_daily
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.document AS
SELECT * FROM core.document
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.entity_relation AS
SELECT * FROM core.entity_relation
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.entity_node_membership AS
SELECT * FROM core.entity_node_membership
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

CREATE VIEW asof.event AS
SELECT * FROM core.event
 WHERE known_at <= asof.current_as_of()
   AND (superseded_at IS NULL OR superseded_at > asof.current_as_of());

-- §4.2 角色与授权。
-- 修正 1：CREATE ROLE 幂等包装——迁移可能在已有角色的库上执行。
-- app_owner 是文档未提及但必需的第三个角色，理由见下面的「属主转移」一段。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_owner') THEN
    CREATE ROLE app_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_read') THEN
    CREATE ROLE app_read;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_write') THEN
    CREATE ROLE app_write;
  END IF;
END $$;

-- 属主转移：把 core / asof / evals / audit 的对象交给非超级用户 app_owner。
--
-- 为什么必需：asof.* 是普通视图，按**视图属主**的身份执行。迁移默认以超级用户
-- 身份运行，视图属主也就是超级用户——而**超级用户无条件绕过 RLS，FORCE 也拦不住**。
-- 属主不换，本文件末尾的行级隔离策略对经由视图的读取就是一纸空文（实测验证：
-- tests/db/test_asof_layer.py::test_private_document_invisible_through_asof_view
-- 在换属主之前，u2 能读到 u1 的私有块）。
--
-- 后续迁移若新建 core/asof 下的对象，必须一并把属主给 app_owner。
-- 忘了会被 check_schema_invariants 的第 6 条不变量抓住。
GRANT USAGE ON SCHEMA core, asof, evals, audit TO app_owner;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT schemaname AS s, tablename AS n FROM pg_tables
            WHERE schemaname IN ('core','evals','audit')
  LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO app_owner', r.s, r.n);
  END LOOP;
  FOR r IN SELECT schemaname AS s, viewname AS n FROM pg_views WHERE schemaname = 'asof'
  LOOP
    EXECUTE format('ALTER VIEW %I.%I OWNER TO app_owner', r.s, r.n);
  END LOOP;
END $$;

ALTER FUNCTION asof.current_as_of() OWNER TO app_owner;

-- 应用角色看不见时点基表
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM app_read;
GRANT USAGE ON SCHEMA core, asof TO app_read;
GRANT SELECT ON ALL TABLES IN SCHEMA asof TO app_read;

-- 修正 2（docs/03-point-in-time.md §4.4）：entity / node_metric / propagation_rule
-- 不是时点表，直接授予 SELECT，否则检索与 Agent 侧连 core.entity 都做不到。
-- 这与 docs/02-data-model.md §9 不变量 5「app_read 对 core 无 SELECT」冲突，
-- 冲突按 docs/10-roadmap.md P0 验收的字面要求（core.fin_fact 被拒）化解：
-- 不变量 5 收窄为「对 bitemporal_registry 登记的七张表无 SELECT」。
GRANT SELECT ON core.entity, core.entity_alias, core.node_metric,
                core.metric_source_map, core.ai_revenue_rule, core.propagation_rule
             TO app_read;

-- 写入中间件用独立角色，且只能 INSERT + 受控 UPDATE
-- 修正 3：补上 USAGE ON SCHEMA core。§4.2 原文只给了 app_read，
-- 没有 USAGE 时下面这条 GRANT INSERT 形同虚设。
GRANT USAGE ON SCHEMA core TO app_write;
GRANT INSERT ON ALL TABLES IN SCHEMA core TO app_write;
GRANT UPDATE (superseded_at) ON core.fin_fact, core.price_daily,
                                core.document, core.doc_block,
                                core.entity_relation, core.entity_node_membership,
                                core.event TO app_write;

-- 评测层：docs/02-data-model.md §1.1 的权限表
GRANT USAGE ON SCHEMA evals TO app_read, app_write;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA evals TO app_read;

-- 审计层：只能追加。
-- 修正 4：docs/02-data-model.md §8 与 docs/09-compliance-security.md §4 都声称
-- 有 REVOKE UPDATE, DELETE，但两份文档都没给 DDL。补在这里。
GRANT USAGE ON SCHEMA audit TO app_read, app_write;
GRANT INSERT, SELECT ON audit.tool_call_log TO app_write;
GRANT SELECT ON audit.tool_call_log TO app_read;
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA audit FROM app_read, app_write, PUBLIC;

-- docs/09-compliance-security.md §3.2 行级隔离。
-- 修正 5（本迁移最重要的一处）：文档原文是 ENABLE + CREATE POLICY ... TO app_read。
-- 那样写，隔离对经由 asof.* 视图的读取**完全不生效**：普通视图按视图属主的身份与
-- RLS 上下文执行，策略只授给 app_read 时根本不会被求值。
-- 正确做法是 ENABLE + FORCE（属主自己也受策略约束）+ 策略授给 PUBLIC。
ALTER TABLE core.document  ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.document  FORCE  ROW LEVEL SECURITY;
ALTER TABLE core.doc_block ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.doc_block FORCE  ROW LEVEL SECURITY;

CREATE POLICY doc_visibility ON core.document FOR SELECT TO PUBLIC
USING (
      (owner_tenant IS NULL AND owner_user IS NULL)                  -- 公共
   OR (owner_tenant = current_setting('app.tenant', true)
       AND owner_user IS NULL)                                       -- 租户私有
   OR (owner_user = current_setting('app.user', true))               -- 用户私有
);

-- doc_block 同样的策略（反规范化的 owner_* 列使其可独立判定，
-- 无需 JOIN document —— 这也是 docs/02-data-model.md §5.3 反规范化的理由之一）
CREATE POLICY block_visibility ON core.doc_block FOR SELECT TO PUBLIC
USING (
      (owner_tenant IS NULL AND owner_user IS NULL)
   OR (owner_tenant = current_setting('app.tenant', true) AND owner_user IS NULL)
   OR (owner_user = current_setting('app.user', true))
);

-- 修正 6：RLS 一旦开启，没有写入策略就等于禁止一切写入——包括写入中间件自己。
-- 归属校验在写入中间件里做（它才知道当前请求属于谁），这里不重复限制，
-- 但 DELETE 刻意不给策略：文档只做版本化，不做物理删除。
CREATE POLICY doc_insert ON core.document FOR INSERT TO PUBLIC WITH CHECK (true);
CREATE POLICY doc_update ON core.document FOR UPDATE TO PUBLIC USING (true) WITH CHECK (true);
CREATE POLICY block_insert ON core.doc_block FOR INSERT TO PUBLIC WITH CHECK (true);
CREATE POLICY block_update ON core.doc_block FOR UPDATE TO PUBLIC USING (true) WITH CHECK (true);
