"""时点安全层：视图 + 角色 + RLS。

这组测试是 P0 的核心验收项（docs/10-roadmap.md P0 最后两行）。
它们证明时点防泄漏是物理生效的，而不只是写在文档里。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
BITEMPORAL_VIEWS = {
    "fin_fact",
    "price_daily",
    "document",
    "doc_block",
    "entity_relation",
    "entity_node_membership",
    "event",
}

# 测试库里的一次性口令，不是任何环境的真实凭据。
APPTEST_PASSWORD = "not-a-real-secret"


@pytest.fixture
def app_read_user() -> str:
    """角色是集群级对象，不随临时库消失，所以每个测试用独立用户名并负责清理。"""
    return f"apptest_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def db(temp_db: str, app_read_user: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(f"""CREATE USER "{app_read_user}" PASSWORD '{APPTEST_PASSWORD}'""")
        conn.execute(f'GRANT app_read TO "{app_read_user}"')
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute(f'DROP OWNED BY "{app_read_user}"')
            conn.execute(f'DROP USER IF EXISTS "{app_read_user}"')
            conn.commit()


def _as_app_read(dsn: str, user: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(dsn, user=user, password=APPTEST_PASSWORD)


@pytest.mark.db
def test_every_bitemporal_table_has_an_asof_view(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = db.execute(
        "SELECT table_name FROM information_schema.views WHERE table_schema='asof'"
    ).fetchall()
    assert {r[0] for r in rows} == BITEMPORAL_VIEWS


@pytest.mark.db
def test_reading_without_as_of_raises_not_returns_empty(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """未设时点必须报错。返回空集是最危险的行为——它让回测静默得出错误结论。"""
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.InvalidParameterValue):
        db.execute("SELECT count(*) FROM asof.fin_fact").fetchone()


@pytest.mark.db
def test_app_read_cannot_touch_base_tables(
    temp_db: str, app_read_user: str, db: psycopg.Connection[tuple[object, ...]]
) -> None:
    """时点表对读角色不授予 SELECT：绕过 asof 视图在数据库层面就做不到。"""
    assert db is not None  # 确保 fixture 已建好角色
    with (
        _as_app_read(temp_db, app_read_user) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        c.execute("SELECT * FROM core.fin_fact")


@pytest.mark.db
def test_app_write_can_only_update_superseded_at(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """列级权限：能打失效标记，改不了任何值字段。"""
    row = db.execute(
        "SELECT has_table_privilege('app_write','core.fin_fact','UPDATE'),"
        "       has_column_privilege('app_write','core.fin_fact','superseded_at','UPDATE'),"
        "       has_column_privilege('app_write','core.fin_fact','value','UPDATE')"
    ).fetchone()
    assert row == (False, True, False)


@pytest.mark.db
def test_rls_enabled_and_forced_on_document_tables(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """FORCE 不能少。

    asof.* 是普通视图，按视图属主的身份执行；只 ENABLE 的话属主自己不受策略约束，
    经视图读取时隔离等于没开。
    """
    rows = db.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace='core'::regnamespace AND relname IN ('document','doc_block') "
        "ORDER BY relname"
    ).fetchall()
    assert rows == [("doc_block", True, True), ("document", True, True)]


def _seed_two_documents(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """一份公共文档、一份 u1 的私有文档，各带一个块。"""
    conn.execute(
        "INSERT INTO core.document "
        "(doc_id, doc_type, title, publish_at, source, content_hash, version_group_id, "
        " owner_user, valid_from, known_at, ingest_run_id) OVERRIDING SYSTEM VALUE VALUES "
        "(1,'policy','公共政策文件','2024-01-01 09:00+08','mock','h-public',1,"
        " NULL,'2024-01-01','2024-01-01 09:00+08','r1'),"
        "(2,'user_upload','u1 的私有材料','2024-01-01 09:00+08','mock','h-private',2,"
        " 'u1','2024-01-01','2024-01-01 09:00+08','r1')"
    )
    conn.execute(
        "INSERT INTO core.doc_block "
        "(doc_id, block_type, ordinal, content, is_leaf, doc_type, publish_at, owner_user,"
        " valid_from, known_at, source, ingest_run_id) VALUES "
        "(1,'paragraph',1,'公共内容',true,'policy','2024-01-01 09:00+08',NULL,"
        " '2024-01-01','2024-01-01 09:00+08','mock','r1'),"
        "(2,'paragraph',1,'私有内容',true,'user_upload','2024-01-01 09:00+08','u1',"
        " '2024-01-01','2024-01-01 09:00+08','mock','r1')"
    )
    conn.commit()


@pytest.mark.db
def test_private_document_invisible_through_asof_view(
    temp_db: str, app_read_user: str, db: psycopg.Connection[tuple[object, ...]]
) -> None:
    """u2 经 asof 视图读取时，看不到 u1 的私有块，只看得到公共块。

    这是 docs/09-compliance-security.md §3.2 那道「第二道防线」真正生效的证明。
    """
    _seed_two_documents(db)

    with _as_app_read(temp_db, app_read_user) as c, c.transaction():
        c.execute("SELECT set_config('app.as_of', '2025-01-01T00:00:00+00:00', true)")
        c.execute("SELECT set_config('app.user', 'u2', true)")
        contents = {r[0] for r in c.execute("SELECT content FROM asof.doc_block").fetchall()}
    assert contents == {"公共内容"}

    with _as_app_read(temp_db, app_read_user) as c, c.transaction():
        c.execute("SELECT set_config('app.as_of', '2025-01-01T00:00:00+00:00', true)")
        c.execute("SELECT set_config('app.user', 'u1', true)")
        contents = {r[0] for r in c.execute("SELECT content FROM asof.doc_block").fetchall()}
    assert contents == {"公共内容", "私有内容"}


@pytest.mark.db
def test_app_write_can_insert_after_rls_enabled(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """RLS 一开就只剩 SELECT 策略的话，写入中间件会被自己的隔离策略锁死。"""
    policies = {
        (str(r[0]), str(r[1]))
        for r in db.execute(
            "SELECT tablename, cmd FROM pg_policies WHERE schemaname='core'"
        ).fetchall()
    }
    for table in ("document", "doc_block"):
        assert (table, "SELECT") in policies
        assert (table, "INSERT") in policies
        assert (table, "UPDATE") in policies


@pytest.mark.db
def test_asof_views_are_not_owned_by_a_superuser(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """超级用户无条件绕过 RLS，FORCE 也拦不住。

    视图属主一旦回到超级用户，上面那条隔离测试就会静默失效——
    它仍然「通过」，因为策略压根不被求值。所以属主本身要有断言看着。
    """
    rows = db.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
        " WHERE c.relnamespace = 'asof'::regnamespace AND r.rolsuper"
    ).fetchall()
    assert rows == []
