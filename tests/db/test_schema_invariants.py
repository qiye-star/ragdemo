"""docs/02-data-model.md §9 的不变量。任何一条失败即 CI 红灯。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.mark.db
def test_all_schema_invariants_hold(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        violations = check_schema_invariants(conn)
    assert violations == []


@pytest.mark.db
def test_invariant_detects_a_missing_column(temp_db: str) -> None:
    """检查器本身要能发现问题，否则它只是个永远返回空列表的摆设。"""
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute("CREATE TABLE core.broken (id int, known_at timestamptz NOT NULL)")
        conn.execute("INSERT INTO core.bitemporal_registry (table_name) VALUES ('core.broken')")
        violations = check_schema_invariants(conn)
    assert any("core.broken" in v for v in violations)


@pytest.mark.db
def test_invariant_detects_app_read_granted_on_a_bitemporal_table(temp_db: str) -> None:
    """不变量 5：读角色对登记在册的时点表不得有 SELECT。

    收窄自文档原文的「对 core schema 无 SELECT」——docs/03 §4.4 明确要求给
    entity / node_metric / propagation_rule 授予 SELECT，两者原本互相矛盾。
    """
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        assert check_schema_invariants(conn) == []
        conn.execute("GRANT SELECT ON core.fin_fact TO app_read")
        violations = check_schema_invariants(conn)
    assert any("app_read" in v and "fin_fact" in v for v in violations)


@pytest.mark.db
def test_invariant_detects_superuser_owned_asof_view(temp_db: str) -> None:
    """不变量 6（新增）：core / asof 的对象不得由超级用户持有。

    超级用户无条件绕过 RLS，属主一退回超级用户，行级隔离就静默失效。
    """
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        assert check_schema_invariants(conn) == []
        conn.execute("ALTER VIEW asof.fin_fact OWNER TO CURRENT_USER")
        violations = check_schema_invariants(conn)
    assert any("超级用户" in v for v in violations)
