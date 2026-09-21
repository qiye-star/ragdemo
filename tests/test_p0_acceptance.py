"""P0 验收：docs/10-roadmap.md P0 表格的七项，逐条可执行。

这个文件跑绿 = P0 可以收。跑红 = 不能进 P1，没有商量余地。

四条计数断言带 `seed_full` 标记，因为它们依赖创始人工作流 W9.1 的全量录入
（实体 100 家、关系 200 条）。**期望值一个都没有改**——改 EXPECTED_COUNTS
去迁就现有数据，等于把验收标准换成「有多少算多少」。
数据补齐后去掉标记即可，或直接跑 `make accept-p0`（它不过滤标记）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

DSN = os.environ.get("RAGDEMO_DSN", "postgresql://postgres:ragdemo@127.0.0.1:5433/ragdemo")

EXPECTED_COUNTS = {
    "core.entity": 100,
    "core.entity_relation": 200,
    "core.propagation_rule": 15,
    "core.node_metric": 30,
}

# 测试库里的一次性口令，不是任何环境的真实凭据。
ACCEPT_PASSWORD = "not-a-real-secret"


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(DSN) as c:
        yield c


@pytest.fixture
def app_read_conn(
    conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """真的建一个授予 app_read 的用户去连接。

    docs/10-roadmap.md P0 的原文是「以 `app_read` 连接执行」，
    查 has_table_privilege 只是看权限表，证明不了连上去真的会被拒。
    """
    user = f"accept_{uuid.uuid4().hex[:10]}"
    conn.execute(f"""CREATE USER "{user}" PASSWORD '{ACCEPT_PASSWORD}'""")
    conn.execute(f'GRANT app_read TO "{user}"')
    conn.commit()
    try:
        with psycopg.connect(DSN, user=user, password=ACCEPT_PASSWORD) as c:
            yield c
    finally:
        conn.rollback()
        conn.execute(f'DROP OWNED BY "{user}"')
        conn.execute(f'DROP USER IF EXISTS "{user}"')
        conn.commit()


@pytest.mark.db
@pytest.mark.seed_full
@pytest.mark.parametrize(("table", "expected"), sorted(EXPECTED_COUNTS.items()))
def test_seed_counts(
    conn: psycopg.Connection[tuple[object, ...]], table: str, expected: int
) -> None:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    assert row[0] == expected


@pytest.mark.db
def test_required_extensions_installed(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    rows = conn.execute("SELECT extname FROM pg_extension").fetchall()
    assert {"pg_search", "vector"} <= {r[0] for r in rows}


@pytest.mark.db
def test_app_read_denied_on_base_table(
    app_read_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """P0 最重要的两条之一：绕过 asof 视图在数据库层面就做不到。"""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_read_conn.execute("SELECT * FROM core.fin_fact")


@pytest.mark.db
def test_asof_view_raises_without_as_of(
    app_read_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """P0 最重要的两条之二：未设时点必须报错，而不是返回空集。

    返回空集是最危险的行为——回测会静默得出错误结论，而且看起来一切正常。
    """
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        app_read_conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()


@pytest.mark.db
def test_asof_view_returns_rows_once_as_of_is_set(
    app_read_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """上一条的对照组：设了时点就能正常读，说明报错不是因为视图坏了。"""
    with app_read_conn.transaction():
        app_read_conn.execute("SELECT set_config('app.as_of', '2025-01-01T00:00:00+00:00', true)")
        row = app_read_conn.execute("SELECT count(*) FROM asof.entity_relation").fetchone()
    assert row is not None
    assert isinstance(row[0], int)
