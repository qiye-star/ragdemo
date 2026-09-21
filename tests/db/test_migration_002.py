"""迁移 002：实体层与指标规则层的关键约束。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        yield conn


def _insert_entity(
    conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    primary: str,
    nodes: list[str],
) -> None:
    conn.execute(
        "INSERT INTO core.entity "
        "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
        "VALUES (%s, %s, 'listed', '算力', 'AI芯片', %s, %s)",
        (entity_id, f"测试-{entity_id}", nodes, primary),
    )


@pytest.mark.db
def test_entity_id_format_is_enforced(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        _insert_entity(db, "688256", "云端训练芯片", ["云端训练芯片"])


@pytest.mark.db
def test_primary_node_must_be_in_l3_node(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        _insert_entity(db, "CN.688256", "先进封装", ["云端训练芯片"])


@pytest.mark.db
def test_relation_cannot_point_at_itself(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with db.transaction(force_rollback=True):
        _insert_entity(db, "CN.688256", "云端训练芯片", ["云端训练芯片"])
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "INSERT INTO core.entity_relation "
                "(from_entity, to_entity, relation_type, valid_from, known_at, source, "
                " ingest_run_id) "
                "VALUES ('CN.688256','CN.688256','supplies_to','2024-01-01','2024-01-01',"
                " 'manual','r1')"
            )


@pytest.mark.db
def test_only_one_live_node_membership_per_pair(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """同一 (实体, 环节) 在任一时刻只能有一行有效——这是基准可复现的前提。"""
    with db.transaction(force_rollback=True):
        _insert_entity(db, "CN.688256", "云端训练芯片", ["云端训练芯片"])
        sql = (
            "INSERT INTO core.entity_node_membership "
            "(entity_id, l3_node, valid_from, known_at, source, ingest_run_id) "
            "VALUES ('CN.688256','云端训练芯片','2024-01-01','2024-01-01','manual','r1')"
        )
        db.execute(sql)
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(sql)
