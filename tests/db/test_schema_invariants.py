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


_DOC_SQL = (
    "INSERT INTO core.document (doc_type, title, publish_at, source, content_hash,"
    " version_group_id, owner_tenant, owner_user, valid_from, known_at, ingest_run_id)"
    " VALUES ('quarterly', 't', '2024-10-28T18:32:00+08', 'mock', 'h1', 0,"
    "         't1', 'u1', '2024-10-28', '2024-10-28T18:32:00+08', 'r1') RETURNING doc_id"
)
# 块的反规范化列先原样复制文档，再在测试里把其中一列改坏。
_BLOCK_SQL = (
    "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, is_leaf, content,"
    " doc_type, publish_at, owner_tenant, owner_user, valid_from, known_at, source,"
    " ingest_run_id)"
    " VALUES (%s, 'paragraph', '', 0, true, 'x', 'quarterly', '2024-10-28T18:32:00+08',"
    "         't1', 'u1', '2024-10-28', '2024-10-28T18:32:00+08', 'mock', 'r1')"
)


@pytest.mark.db
@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        # 05-document-pipeline.md §6 规则 5 点名的全部反规范化列。owner_* 最要紧：
        # RLS 的 block_visibility 策略读的是块自己的 owner 列，不看文档——私有文档
        # 下一个 owner_user 为 NULL 的块会被当成公共数据检索出来。
        ("owner_user", None),
        ("owner_tenant", None),
        ("publish_at", "2024-10-27T00:00:00+08"),
        ("entity_id", None),
        ("doc_type", "annual_report"),
        ("known_at", "2024-10-29T00:00:00+08"),
    ],
)
def test_invariant_detects_block_denormalized_column_diverging_from_document(
    temp_db: str, column: str, bad_value: object
) -> None:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
            " l2_segment, l3_node, primary_node)"
            " VALUES ('CN.688041', '海光信息', 'listed', '算力', 'AI芯片',"
            " ARRAY['云端训练芯片'], '云端训练芯片')"
        )
        row = conn.execute(_DOC_SQL).fetchone()
        assert row is not None
        doc_id = row[0]
        conn.execute(
            "UPDATE core.document SET entity_id = 'CN.688041' WHERE doc_id = %s", (doc_id,)
        )
        conn.execute(_BLOCK_SQL, (doc_id,))
        conn.execute("UPDATE core.doc_block SET entity_id = 'CN.688041'")
        assert check_schema_invariants(conn) == [], "前置条件：原样复制时不应有违规"

        conn.execute(f"UPDATE core.doc_block SET {column} = %s", (bad_value,))
        violations = check_schema_invariants(conn)
    assert any("反规范化" in v for v in violations), (column, violations)
