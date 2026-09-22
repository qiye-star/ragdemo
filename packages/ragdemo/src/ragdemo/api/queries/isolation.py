"""权限隔离探针：证明隔离机制确实生效，而不是展示私有内容。

约束 6（`owner_user IS NOT NULL` 的块硬编码不可见）与 ADR-0010:45「硬编码
只看公共检索空间」意味着这个模块**不能**做成"切换身份浏览私有文档"——
那正是诊断界面结构上不允许存在的东西。这里改用固定探针矩阵：身份来自
硬编码的 `PROBES` 元组（不接受请求参数），每个分支只返回聚合计数与
布尔判定，一个文本列都不取。看到的是"隔离机制生效与否"，永远看不到
私有内容本身。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import psycopg

from ragdemo.api.schemas import (
    AsofViewOwner,
    CatalogResponse,
    CheckResult,
    ConnectionIdentity,
    ErrorBranch,
    IdentityBranch,
    PolicyDefinition,
    ProbesResponse,
    RelationCounts,
    RlsStatus,
)
from ragdemo.api.serialize import as_bool, as_int, as_optional_str, as_str, as_str_list
from ragdemo.seed.isolation_demo import DEMO_TENANT, DEMO_USER_A, DEMO_USER_B
from ragdemo_core.db.session import as_of_session


@dataclass(frozen=True)
class Probe:
    key: str
    label: str
    tenant: str | None
    user: str | None


# 固定矩阵，与请求无关——四个分支能展示出来，是因为身份来自这里，
# 不是来自请求参数。带 `&tenant=&user=` 查询参数发这条路由的响应必须
# 逐字节不变（tests/api/test_isolation_api.py 断言这一点）。
PROBES: Final[tuple[Probe, ...]] = (
    Probe("public", "公共空间（无租户、无用户）", None, None),
    Probe("tenant", "租户上下文", DEMO_TENANT, None),
    Probe("user_a", "用户 A 上下文", DEMO_TENANT, DEMO_USER_A),
    Probe("user_b", "用户 B 上下文", DEMO_TENANT, DEMO_USER_B),
)

_COUNT_SQL_TEMPLATE = """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE owner_tenant IS NULL AND owner_user IS NULL) AS public_rows,
           count(*) FILTER (WHERE owner_user IS NOT NULL) AS user_private_rows,
           count(*) FILTER (
               WHERE owner_tenant IS NOT NULL AND owner_user IS NULL
           ) AS tenant_private_rows,
           count(*) FILTER (WHERE owner_user = '') AS empty_owner_rows,
           count(*) FILTER (
               WHERE owner_user IS NOT NULL
                 AND owner_user <> coalesce(current_setting('app.user', true), '')
           ) AS foreign_user_rows,
           count(*) FILTER (
               WHERE owner_tenant IS NOT NULL
                 AND owner_tenant <> coalesce(current_setting('app.tenant', true), '')
           ) AS foreign_tenant_rows
      FROM {relation}
"""


def _row_to_counts(row: tuple[object, ...]) -> RelationCounts:
    return RelationCounts(
        total=as_int(row[0]),
        public_rows=as_int(row[1]),
        user_private_rows=as_int(row[2]),
        tenant_private_rows=as_int(row[3]),
        empty_owner_rows=as_int(row[4]),
        foreign_user_rows=as_int(row[5]),
        foreign_tenant_rows=as_int(row[6]),
    )


def _run_identity_branch(
    conn: psycopg.Connection[tuple[object, ...]], probe: Probe, as_of: datetime
) -> IdentityBranch:
    """一个独立的顶层事务：`as_of_session` 内部用 `conn.transaction()`
    开启，退出即提交/回滚，不与其余五个分支共享事务边界。"""
    with as_of_session(conn, as_of, tenant=probe.tenant, user=probe.user):
        doc_row = conn.execute(_COUNT_SQL_TEMPLATE.format(relation="asof.document")).fetchone()
        block_row = conn.execute(_COUNT_SQL_TEMPLATE.format(relation="asof.doc_block")).fetchone()
    assert doc_row is not None
    assert block_row is not None
    return IdentityBranch(
        key=probe.key,
        label=probe.label,
        tenant=probe.tenant,
        user=probe.user,
        documents=_row_to_counts(doc_row),
        blocks=_row_to_counts(block_row),
    )


def _missing_as_of_branch(conn: psycopg.Connection[tuple[object, ...]]) -> ErrorBranch:
    """不设 as_of 直接查 asof 视图——期望 22023（约束 4 的运行时告警器）。

    显式 `set_config('app.as_of', '', true)` 而不是"什么都不做"：连接是
    autocommit，每条语句各自成事务，GUC 本来就没设；写空串同时覆盖
    "没设"与"设成空串"两种情况，正对上 `asof.current_as_of()` 里
    `v IS NULL OR v = ''` 的双判。异常在 `conn.transaction()` 块内抛出，
    退出时自动 ROLLBACK，连接随即恢复可用——不能把 try/except 包在事务块
    外面再手动处理，那样异常发生时事务不会正确回滚。
    """
    try:
        with conn.transaction():
            conn.execute("SELECT set_config('app.as_of', '', true)")
            conn.execute("SELECT count(*) FROM asof.doc_block")
    except psycopg.errors.InvalidParameterValue as exc:
        return ErrorBranch(
            key="missing_as_of",
            raised=True,
            sqlstate=exc.sqlstate,
            message_head=str(exc).splitlines()[0],
            passed=exc.sqlstate == "22023",
        )
    return ErrorBranch(
        key="missing_as_of", raised=False, sqlstate=None, message_head=None, passed=False
    )


def _base_table_branch(conn: psycopg.Connection[tuple[object, ...]]) -> ErrorBranch:
    """误查 core.* 基表——期望 42501（约束 5 的运行时告警器）。

    在超级用户误配置（开发模式忘了 SET ROLE）下这个分支会**成功**，
    于是 passed=false 亮红——这正是要的运行时体检：约束 5 的机械保证
    只在 app_diag 身份下成立，超级用户连接下它本来就不该假装安全。
    """
    try:
        with conn.transaction():
            conn.execute("SELECT count(*) FROM core.doc_block")
    except psycopg.errors.InsufficientPrivilege as exc:
        return ErrorBranch(
            key="base_table_denied",
            raised=True,
            sqlstate=exc.sqlstate,
            message_head=str(exc).splitlines()[0],
            passed=exc.sqlstate == "42501",
        )
    return ErrorBranch(
        key="base_table_denied", raised=False, sqlstate=None, message_head=None, passed=False
    )


def _status(condition: bool) -> str:
    return "passed" if condition else "failed"


def _compute_checks(branches: list[IdentityBranch]) -> tuple[list[CheckResult], bool]:
    by_key = {b.key: b for b in branches}
    public = by_key["public"]
    tenant = by_key.get("tenant")
    user_a = by_key.get("user_a")

    # demo_seeded 是状态位，不是断言——它只回答"这次演示数据存不存在"，
    # 后面几条私有相关的检查据此判定要不要标 skipped：没有私有数据时
    # foreign_user_rows 之类的计数恒为 0，"passed" 会是一句没有测试过
    # 任何东西的假绿；标 skipped 更诚实。
    demo_seeded = user_a is not None and user_a.documents.user_private_rows > 0

    no_foreign_user = all(
        b.documents.foreign_user_rows == 0 and b.blocks.foreign_user_rows == 0 for b in branches
    )
    no_foreign_tenant = all(
        b.documents.foreign_tenant_rows == 0 and b.blocks.foreign_tenant_rows == 0 for b in branches
    )
    no_empty_owner = all(
        b.documents.empty_owner_rows == 0 and b.blocks.empty_owner_rows == 0 for b in branches
    )
    public_sees_no_private = (
        public.documents.user_private_rows == 0
        and public.documents.tenant_private_rows == 0
        and public.blocks.user_private_rows == 0
        and public.blocks.tenant_private_rows == 0
    )
    tenant_sees_no_user_private = tenant is None or (
        tenant.documents.user_private_rows == 0 and tenant.blocks.user_private_rows == 0
    )
    monotonic = all(
        b.documents.total >= public.documents.total and b.blocks.total >= public.blocks.total
        for b in branches
    )
    not_silently_empty = public.documents.total > 0

    checks = [
        CheckResult(
            name="demo_seeded", status=_status(demo_seeded), detail=f"演示数据存在: {demo_seeded}"
        ),
        CheckResult(
            name="no_foreign_user_row",
            status=_status(no_foreign_user) if demo_seeded else "skipped",
            detail="任何分支看见的私有行，owner_user 必须恰好是本分支身份",
        ),
        CheckResult(
            name="no_foreign_tenant_row",
            status=_status(no_foreign_tenant) if demo_seeded else "skipped",
            detail="任何分支看见的租户私有行，owner_tenant 必须恰好是本分支租户",
        ),
        CheckResult(
            name="public_sees_no_private",
            status=_status(public_sees_no_private) if demo_seeded else "skipped",
            detail="公共分支不应看见任何私有/租户行",
        ),
        CheckResult(
            name="tenant_sees_no_user_private",
            status=_status(tenant_sees_no_user_private) if demo_seeded else "skipped",
            detail="租户分支不应看见用户私有行",
        ),
        CheckResult(
            name="owner_user_never_empty_string",
            status=_status(no_empty_owner) if demo_seeded else "skipped",
            detail="owner_user/owner_tenant 为空字符串会匹配 RLS 的默认值，等于向所有人开放",
        ),
        CheckResult(
            name="monotonic_vs_public",
            status=_status(monotonic),
            detail="加身份只能让可见行数增加，不能减少",
        ),
        CheckResult(
            name="not_silently_empty",
            status=_status(not_silently_empty),
            detail="公共分支总行数必须大于 0，防止「全都看不见」被误读成「隔离生效」",
        ),
    ]
    return checks, demo_seeded


def run_probes(conn: psycopg.Connection[tuple[object, ...]], as_of: datetime) -> ProbesResponse:
    """六个各自独立的顶层事务，顺序固定：先四个读分支，再两个故意报错的
    分支——这样即使某天错误分支把连接搞坏了，前四个分支的数据也已经取到。
    """
    branches = [_run_identity_branch(conn, p, as_of) for p in PROBES]
    error_branches = [_missing_as_of_branch(conn), _base_table_branch(conn)]
    checks, demo_seeded = _compute_checks(branches)
    return ProbesResponse(
        as_of=as_of.isoformat(),
        demo_seeded=demo_seeded,
        identity_branches=branches,
        checks=checks,
        error_branches=error_branches,
    )


_POLICIES_SQL = """
    SELECT schemaname, tablename, policyname, cmd, roles::text[], qual, with_check
      FROM pg_policies WHERE schemaname = 'core' AND tablename IN ('document', 'doc_block')
     ORDER BY tablename, cmd, policyname
"""

_RLS_STATUS_SQL = """
    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, r.rolname, r.rolsuper
      FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
     WHERE c.relnamespace = 'core'::regnamespace AND c.relname IN ('document', 'doc_block')
     ORDER BY c.relname
"""

_ASOF_VIEW_OWNERS_SQL = """
    SELECT c.relname, r.rolname, r.rolsuper
      FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
     WHERE c.relnamespace = 'asof'::regnamespace AND c.relkind = 'v'
     ORDER BY c.relname
"""

_CONNECTION_IDENTITY_SQL = """
    SELECT current_user, session_user,
           coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false),
           has_table_privilege(current_user, 'asof.doc_block', 'SELECT'),
           has_table_privilege(current_user, 'core.doc_block', 'SELECT'),
           has_table_privilege(current_user, 'core.doc_block', 'INSERT'),
           has_table_privilege(current_user, 'core.document', 'SELECT'),
           has_table_privilege(current_user, 'quality.quality_metric', 'SELECT')
"""


def get_catalog(
    conn: psycopg.Connection[tuple[object, ...]], *, as_of: datetime
) -> CatalogResponse:
    """隔离机制是否真在生效的目录佐证——原样回传 `pg_policies` 的策略原文
    与 `relforcerowsecurity`/属主超级用户判定，不加解读。这不是探针矩阵的
    补充说明，是它的前置条件：`relforcerowsecurity=false` 或
    `owner_is_superuser=true` 任一成立，前面四个身份分支的绿全是假的。
    """
    policy_rows = conn.execute(_POLICIES_SQL).fetchall()
    rls_rows = conn.execute(_RLS_STATUS_SQL).fetchall()
    view_rows = conn.execute(_ASOF_VIEW_OWNERS_SQL).fetchall()
    identity_row = conn.execute(_CONNECTION_IDENTITY_SQL).fetchone()
    assert identity_row is not None

    return CatalogResponse(
        as_of=as_of.isoformat(),
        # 这条查询完全不看 as_of——目录信息是数据库结构状态，不随时点变化；
        # 参数仍然必填（约束 4 不留例外），这个字段如实标注它对结果无影响。
        as_of_affects_result=False,
        policies=[
            PolicyDefinition(
                schemaname=as_str(r[0]),
                tablename=as_str(r[1]),
                policyname=as_str(r[2]),
                cmd=as_str(r[3]),
                roles=as_str_list(r[4]),
                qual=as_optional_str(r[5]),
                with_check=as_optional_str(r[6]),
            )
            for r in policy_rows
        ],
        rls_status=[
            RlsStatus(
                relname=as_str(r[0]),
                relrowsecurity=as_bool(r[1]),
                relforcerowsecurity=as_bool(r[2]),
                owner=as_str(r[3]),
                owner_is_superuser=as_bool(r[4]),
            )
            for r in rls_rows
        ],
        asof_view_owners=[
            AsofViewOwner(
                relname=as_str(r[0]), owner=as_str(r[1]), owner_is_superuser=as_bool(r[2])
            )
            for r in view_rows
        ],
        connection_identity=ConnectionIdentity(
            current_user=as_str(identity_row[0]),
            session_user=as_str(identity_row[1]),
            current_user_is_superuser=as_bool(identity_row[2]),
            asof_doc_block_select=as_bool(identity_row[3]),
            core_doc_block_select=as_bool(identity_row[4]),
            core_doc_block_insert=as_bool(identity_row[5]),
            core_document_select=as_bool(identity_row[6]),
            quality_metric_select=as_bool(identity_row[7]),
        ),
    )


__all__ = ("PROBES", "Probe", "get_catalog", "run_probes")
