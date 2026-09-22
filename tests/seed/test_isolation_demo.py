"""权限隔离演示种子：公共对照 + 租户私有 + 用户 A/B 私有。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.seed.isolation_demo import (
    DEMO_SOURCE,
    DEMO_TENANT,
    DEMO_USER_A,
    DEMO_USER_B,
    SYNTHETIC_MARK,
    AlreadySeeded,
    NotLoopbackHost,
    clear_isolation_demo,
    seed_isolation_demo,
)
from ragdemo_core.db.invariants import check_point_in_time_leaks, check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> psycopg.Connection[tuple[object, ...]]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    return c


@pytest.mark.db
def test_seed_creates_three_private_documents_and_one_public_control(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    doc_ids = seed_isolation_demo(conn, dsn=temp_db)
    assert set(doc_ids.keys()) == {"public", "tenant", "user_a", "user_b"}

    rows = conn.execute(
        "SELECT doc_id, owner_tenant, owner_user FROM core.document"
        " WHERE source = %s ORDER BY doc_id",
        (DEMO_SOURCE,),
    ).fetchall()
    assert len(rows) == 4

    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id[doc_ids["public"]] == (None, None)
    assert by_id[doc_ids["tenant"]] == (DEMO_TENANT, None)
    assert by_id[doc_ids["user_a"]] == (DEMO_TENANT, DEMO_USER_A)
    assert by_id[doc_ids["user_b"]] == (DEMO_TENANT, DEMO_USER_B)

    # owner_user 永不为空字符串——空串会匹配 RLS 里 current_setting('app.user','')
    # 的默认值，等于向所有人开放（web-diagnostic-ui 计划的坑 6）。
    assert all(r[2] != "" for r in rows)
    assert all(r[1] != "" for r in rows)

    (block_count,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    assert block_count == 4


@pytest.mark.db
def test_seed_is_idempotent_and_raises_already_seeded(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    seed_isolation_demo(conn, dsn=temp_db)
    with pytest.raises(AlreadySeeded):
        seed_isolation_demo(conn, dsn=temp_db)


@pytest.mark.db
def test_seed_with_force_clears_and_reseeds(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    first = seed_isolation_demo(conn, dsn=temp_db)
    second = seed_isolation_demo(conn, dsn=temp_db, force=True)
    # 强制重置后 doc_id 不必相同（IDENTITY 递增），但 key 集合与数量必须一致。
    assert set(first.keys()) == set(second.keys())
    (doc_count,) = conn.execute(
        "SELECT count(*) FROM core.document WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    assert doc_count == 4


@pytest.mark.db
def test_clear_removes_every_seeded_row(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    seed_isolation_demo(conn, dsn=temp_db)
    clear_isolation_demo(conn, dsn=temp_db)
    (doc_count,) = conn.execute(
        "SELECT count(*) FROM core.document WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    (block_count,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    assert doc_count == 0
    assert block_count == 0


@pytest.mark.db
def test_clear_is_a_noop_when_nothing_was_seeded(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    # rowcount 断言比较的是"预期删除数"与"实际删除数"，两者都为 0 时应当
    # 静默通过——这不是 RLS 拒绝导致的哑炮，是真的没有数据可删。
    clear_isolation_demo(conn, dsn=temp_db)


@pytest.mark.db
def test_seeded_data_passes_db_check(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    """演示数据不得把 `ragdemo db check` 搞红——这是种子存在的前提，
    不是可选项：一个会破坏系统自身完整性检查的诊断工具毫无诊断价值。"""
    import datetime as dt

    seed_isolation_demo(conn, dsn=temp_db)
    assert check_schema_invariants(conn) == []
    assert check_point_in_time_leaks(conn, dt.datetime.now(dt.UTC)) == {}


def test_seed_refuses_non_loopback_dsn(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    remote_dsn = "postgresql://postgres:ragdemo@some-remote-host:5433/ragdemo"
    with pytest.raises(NotLoopbackHost):
        seed_isolation_demo(conn, dsn=remote_dsn)


def test_clear_refuses_non_loopback_dsn(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    remote_dsn = "postgresql://postgres:ragdemo@some-remote-host:5433/ragdemo"
    with pytest.raises(NotLoopbackHost):
        clear_isolation_demo(conn, dsn=remote_dsn)


@pytest.mark.db
def test_synthetic_mark_appears_in_every_seeded_title(
    conn: psycopg.Connection[tuple[object, ...]], temp_db: str
) -> None:
    seed_isolation_demo(conn, dsn=temp_db)
    rows = conn.execute(
        "SELECT title FROM core.document WHERE source = %s", (DEMO_SOURCE,)
    ).fetchall()
    titles = [str(r[0]) for r in rows]
    assert len(titles) == 4
    assert all(SYNTHETIC_MARK in t for t in titles)
