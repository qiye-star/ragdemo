"""app_diag：诊断接口专用只读角色（db/migrations/016_api_read_role.sql）。

这组测试证明「后端物理上无法写入」「只查 asof 视图」不是靠代码自觉，
是数据库机械拒绝——与 tests/db/test_asof_layer.py 同一份验收精神，
针对新角色 app_diag 单独立一组，不与 app_read 的测试混在一起。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

BITEMPORAL_BASE_TABLES = (
    "core.document",
    "core.doc_block",
    "core.fin_fact",
    "core.price_daily",
    "core.entity_relation",
    "core.entity_node_membership",
    "core.event",
)

# 测试库里的一次性口令，不是任何环境的真实凭据。
APITEST_PASSWORD = "not-a-real-secret"


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.commit()
        yield conn


@pytest.fixture
def api_login_user() -> str:
    """角色是集群级对象，不随临时库消失，所以每个测试用独立用户名并负责清理。"""
    return f"apidiag_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def api_conn(
    db: psycopg.Connection[tuple[object, ...]], temp_db: str, api_login_user: str
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    db.execute(
        sql.SQL("CREATE USER {} PASSWORD {}").format(
            sql.Identifier(api_login_user), sql.Literal(APITEST_PASSWORD)
        )
    )
    db.execute(sql.SQL("GRANT app_diag TO {}").format(sql.Identifier(api_login_user)))
    db.commit()
    try:
        conn = psycopg.connect(temp_db, user=api_login_user, password=APITEST_PASSWORD)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(api_login_user)))
        db.execute(sql.SQL("DROP USER IF EXISTS {}").format(sql.Identifier(api_login_user)))
        db.commit()


@pytest.mark.db
def test_app_diag_cannot_select_any_bitemporal_base_table(
    api_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    for qualified in BITEMPORAL_BASE_TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.SQL(qualified)))
        api_conn.rollback()


@pytest.mark.db
def test_app_diag_has_no_write_privilege_anywhere(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = db.execute(
        "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = 'app_diag'"
    ).fetchall()
    granted = {r[0] for r in rows}
    assert granted <= {"SELECT"}, f"app_diag 被授予了非 SELECT 权限: {granted}"


@pytest.mark.db
def test_app_diag_has_at_least_one_grant(db: psycopg.Connection[tuple[object, ...]]) -> None:
    # 上一条测试对「空授权集合」也会通过（空集合 <= {"SELECT"}）——
    # 这条防迁移写错导致 app_diag 实际上什么都查不到、上一条测试假绿。
    row = db.execute(
        "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = 'app_diag'"
    ).fetchone()
    assert row is not None
    count = row[0]
    assert isinstance(count, int) and count > 0


@pytest.mark.db
def test_every_asof_view_is_selectable_by_app_diag(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    views = [
        r[0]
        for r in db.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'asof'"
        ).fetchall()
    ]
    assert views, "asof schema 下没有任何视图，夹具本身有问题"
    for view in views:
        (readable,) = db.execute(
            "SELECT has_table_privilege('app_diag', %s, 'SELECT')", (f"asof.{view}",)
        ).fetchone()  # type: ignore[misc]
        assert readable, f"app_diag 对 asof.{view} 没有 SELECT 权限"


@pytest.mark.db
def test_set_role_fallback_denies_base_tables(
    db: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    """开发模式（RAGDEMO_API_ALLOW_PRIVILEGED_DSN）用超级用户连接后 SET ROLE
    app_diag——这条证明那条退路本身是合法的：SET ROLE 之后，current_user
    的身份判定生效，超级用户连接也照样被拒。"""
    conn = psycopg.connect(temp_db)
    try:
        conn.execute("SET ROLE app_diag")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM core.doc_block")
    finally:
        conn.close()
