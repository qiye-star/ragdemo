"""P0 验收：docs/10-roadmap.md P0 表格的七项，逐条可执行。

这个文件跑绿 = P0 可以收。跑红 = 不能进 P1，没有商量余地。

四条计数断言是**下限**（2026-09-28 负责人决定，原为 == 100 / 200 / 15 / 30）：
种子改由 `docs/csv` 生成（`ragdemo db seed-build`），底稿 200 条关系里有 28 条的一端是
"北美云厂"这类泛称、3 条合并别名后是同一实体，建不成实体关系；实体因补录关系表里的
名单外公司而多于 100。关系下限因此降到 150，其余三项不降。
只数"现在有效"的行：现行关系、启用的规则、有现行口径版本的指标——同步导入时被
标记失效 / 停用的旧行留在表里（时点表不删行），不该算进"有多少数据"。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

DSN = os.environ.get("RAGDEMO_DSN", "postgresql://postgres:ragdemo@127.0.0.1:5433/ragdemo")

MINIMUM_COUNTS = {
    "core.entity": ("SELECT count(*) FROM core.entity", 100),
    "core.entity_relation": (
        "SELECT count(*) FROM core.entity_relation WHERE superseded_at IS NULL",
        150,
    ),
    "core.propagation_rule": ("SELECT count(*) FROM core.propagation_rule WHERE enabled", 15),
    "core.node_metric": (
        "SELECT count(*) FROM core.node_metric_version WHERE superseded_at IS NULL",
        30,
    ),
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
@pytest.mark.parametrize("table", sorted(MINIMUM_COUNTS))
def test_seed_counts(conn: psycopg.Connection[tuple[object, ...]], table: str) -> None:
    sql, minimum = MINIMUM_COUNTS[table]
    row = conn.execute(sql).fetchone()
    assert row is not None
    assert int(str(row[0])) >= minimum, f"{table}: {row[0]} < {minimum}"


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
