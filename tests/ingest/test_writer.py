"""时点写入中间件：幂等、更正、乱序拒绝。

这组测试是 P1 正确性的核心。写错了，历史库会被污染，而历史库按设计不可删。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import FactRecord
from ragdemo.ingest.writer import (
    OutOfOrderCorrection,
    PointInTimeWriter,
    UnknownEntityRef,
    WriteOutcome,
)
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def _record(value: float, known_at: datetime, ref: str = "688256.SH") -> FactRecord:
    return FactRecord(
        entity_ref=ref,
        metric_field="revenue_total",
        period="2024Q3",
        period_end=date(2024, 9, 30),
        value=value,
        unit="CNY",
        currency="CNY",
        valid_from=date(2024, 7, 1),
        known_at=known_at,
        source_ref="x",
    )


@pytest.fixture()
def writer(temp_db: str) -> PointInTimeWriter:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.execute(
        "INSERT INTO core.node_metric (metric_id, metric_name, metric_role,"
        " frequency, source_type, definition, unit) "
        "VALUES ('revenue_total','营业收入','confirming','quarterly','filing','合并口径','CNY')"
    )
    conn.execute(
        "INSERT INTO core.metric_source_map (metric_id, provider, provider_field) "
        "VALUES ('revenue_total','tushare','revenue_total')"
    )
    conn.commit()
    return PointInTimeWriter(conn, ingest_run_id="r1", source="tushare")


@pytest.mark.db
def test_first_write_inserts(writer: PointInTimeWriter) -> None:
    outcome = writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    assert outcome is WriteOutcome.INSERTED


@pytest.mark.db
def test_identical_rewrite_is_skipped_not_duplicated(writer: PointInTimeWriter) -> None:
    """幂等：Dagster 重跑同一分区不得产生第二行。"""
    rec = _record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC))
    assert writer.write_fact(rec) is WriteOutcome.INSERTED
    assert writer.write_fact(rec) is WriteOutcome.SKIPPED_IDENTICAL
    (n,) = writer.conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_changed_value_triggers_correction(writer: PointInTimeWriter) -> None:
    """更正 = 给旧行打 superseded_at + 插新行，两行共存。"""
    writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    corrected_at = datetime(2025, 1, 15, 19, 0, tzinfo=UTC)
    assert writer.write_fact(_record(12500.0, corrected_at)) is WriteOutcome.CORRECTED

    rows = writer.conn.execute(
        "SELECT value, superseded_at FROM core.fin_fact ORDER BY known_at"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == corrected_at, "旧行的 superseded_at 必须等于新行的 known_at"
    assert rows[1][1] is None


@pytest.mark.db
def test_older_known_at_is_rejected(writer: PointInTimeWriter) -> None:
    """乱序到达：不能用更早可知的数据去覆盖更晚可知的数据。"""
    writer.write_fact(_record(12500.0, datetime(2025, 1, 15, 19, 0, tzinfo=UTC)))
    with pytest.raises(OutOfOrderCorrection):
        writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))


@pytest.mark.db
def test_unknown_entity_ref_raises_and_writes_nothing(writer: PointInTimeWriter) -> None:
    with pytest.raises(UnknownEntityRef):
        writer.write_fact(_record(1.0, datetime(2024, 10, 28, 18, 32, tzinfo=UTC), ref="999999.SH"))
    (n,) = writer.conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert n == 0


@pytest.mark.db
def test_ingested_at_differs_from_known_at(writer: PointInTimeWriter) -> None:
    """known_at 来自数据本身，ingested_at 是 now()。两者混同会毁掉回填。"""
    writer.write_fact(_record(12340.5, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    known_at, ingested_at = writer.conn.execute(
        "SELECT known_at, ingested_at FROM core.fin_fact"
    ).fetchone()  # type: ignore[misc]
    assert known_at < ingested_at  # type: ignore[operator]


@pytest.mark.db
def test_write_facts_reports_counts_per_outcome(writer: PointInTimeWriter) -> None:
    t = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    counts = writer.write_facts([_record(12340.5, t), _record(12340.5, t)])
    assert counts == {WriteOutcome.INSERTED: 1, WriteOutcome.SKIPPED_IDENTICAL: 1}
