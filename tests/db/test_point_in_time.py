"""docs/03-point-in-time.md §5 的五条泄漏自检 + 视图的时点过滤行为。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.invariants import check_point_in_time_leaks
from ragdemo_core.db.migrate import migrate
from ragdemo_core.db.session import as_of_session

MIGRATIONS = Path("db/migrations")
PROBE = datetime(2025, 1, 1, tzinfo=UTC)


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
            "VALUES ('revenue_total','营业收入','confirming','quarterly','filing','合并口径','CNY')"
        )
        conn.commit()
        yield conn


_FACT = (
    "INSERT INTO core.fin_fact "
    "(entity_id, metric_id, period, period_end, value, unit, valid_from, known_at,"
    " source, ingest_run_id) "
    "VALUES ('CN.688256','revenue_total','2024Q3','2024-09-30',1,'CNY','2024-07-01',"
    " %s,'tushare','r1')"
)


@pytest.mark.db
def test_clean_database_has_no_leaks(db: psycopg.Connection[tuple[object, ...]]) -> None:
    assert check_point_in_time_leaks(db, PROBE) == {}


@pytest.mark.db
def test_leak_check_detects_known_at_before_period_end(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """known_at 早于期末 = 提前知道了还没发生的事。"""
    db.execute(_FACT, ("2024-08-01 00:00+08",))
    db.commit()
    assert check_point_in_time_leaks(db, PROBE)["known_at_before_period_end"] == 1


@pytest.mark.db
def test_leak_check_detects_opinion_citing_a_future_block(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """引用「生成时还不存在的块」是最难靠人眼发现的一类泄漏。"""
    db.execute(
        "INSERT INTO core.document "
        "(doc_id, entity_id, doc_type, title, publish_at, source, content_hash,"
        " version_group_id, valid_from, known_at, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES "
        "(1,'CN.688256','quarterly','三季报','2024-10-28 18:32+08','mock','h1',1,"
        " '2024-07-01','2024-10-28 18:32+08','r1')"
    )
    db.execute(
        "INSERT INTO core.doc_block "
        "(block_id, doc_id, block_type, ordinal, content, is_leaf, entity_id, doc_type,"
        " publish_at, valid_from, known_at, source, ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES "
        "(1,1,'paragraph',1,'三季报原文',true,'CN.688256','quarterly',"
        " '2024-10-28 18:32+08','2024-07-01','2024-10-28 18:32+08','mock','r1')"
    )
    db.execute(
        "INSERT INTO core.opinion "
        "(entity_id, direction, confidence, thesis, evidence_blocks, agent_name,"
        " prompt_version, model, run_id, as_of) "
        "VALUES ('CN.688256','bull','medium','用了还没发布的三季报',ARRAY[1]::bigint[],"
        " 'fundamental','v1','m','r1','2024-10-01 09:00+08')"
    )
    db.commit()
    assert check_point_in_time_leaks(db, PROBE)["opinion_cites_future_block"] == 1


@pytest.mark.db
def test_asof_view_hides_future_rows(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """视图的核心行为：as_of 之后才可知的数据必须看不见。"""
    db.execute(_FACT, ("2024-10-28 18:32+08",))
    db.commit()

    with as_of_session(db, datetime(2024, 10, 1, tzinfo=UTC)) as conn:
        row_before = conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()
    with as_of_session(db, datetime(2024, 11, 1, tzinfo=UTC)) as conn:
        row_after = conn.execute("SELECT count(*) FROM asof.fin_fact").fetchone()

    assert row_before is not None
    assert row_after is not None
    assert (row_before[0], row_after[0]) == (0, 1)
