"""迁移 003：时点事实层。重点验证「更正必须走 superseded_at」是数据库强制的。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_FACT = (
    "INSERT INTO core.fin_fact "
    "(entity_id, metric_id, period, period_end, value, unit, valid_from, known_at, "
    " source, ingest_run_id) "
    "VALUES ('CN.688256','revenue_total','2024Q3','2024-09-30',%s,'CNY','2024-07-01',%s,"
    " 'tushare','r1')"
)


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(
            "INSERT INTO core.entity "
            "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
            "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
            " ARRAY['云端训练芯片'],'云端训练芯片')"
        )
        conn.execute(
            "INSERT INTO core.node_metric "
            "(metric_id, metric_name, metric_role, frequency, source_type, definition, unit) "
            "VALUES ('revenue_total','营业收入','confirming','quarterly','filing',"
            " '合并报表营业收入','CNY')"
        )
        conn.commit()
        yield conn


@pytest.mark.db
def test_second_live_row_for_same_period_is_rejected(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """忘记给旧行打 superseded_at 就插新行 —— 唯一索引必须拦下来。"""
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(_FACT, (12500.0, "2025-01-15 19:00+08"))


@pytest.mark.db
def test_correction_flow_succeeds(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """按 docs/03-point-in-time.md §3 的流程走就应该成功，且两行共存。"""
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        db.execute(
            "UPDATE core.fin_fact SET superseded_at = %s "
            "WHERE entity_id='CN.688256' AND metric_id='revenue_total' "
            "  AND period='2024Q3' AND superseded_at IS NULL",
            ("2025-01-15 19:00+08",),
        )
        db.execute(_FACT, (12500.0, "2025-01-15 19:00+08"))
        row = db.execute("SELECT count(*) FROM core.fin_fact").fetchone()
    assert row is not None
    assert row[0] == 2


@pytest.mark.db
def test_superseded_at_must_be_after_known_at(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    with db.transaction(force_rollback=True):
        db.execute(_FACT, (12340.5, "2024-10-28 18:32+08"))
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "UPDATE core.fin_fact SET superseded_at = '2024-01-01 00:00+08' "
                "WHERE superseded_at IS NULL"
            )
