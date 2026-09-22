"""时点写入中间件：幂等、更正、乱序拒绝。

这组测试是 P1 正确性的核心。写错了，历史库会被污染，而历史库按设计不可删。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.base import FactRecord
from ragdemo.adapters.tushare import PriceRow
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


@pytest.mark.db
def test_scale_factor_converts_provider_unit_before_write_and_compare(
    temp_db: str,
) -> None:
    """metric_source_map.scale_factor 是「供应商单位 -> 本系统单位」的换算系数。

    这里模拟一个用元（而不是本系统的万元）报数的供应商：scale_factor=0.0001。
    一条原始值 1234050000.0（元）的记录写库后，存的应该是换算过的
    123405.0（万元），不是原始的元值——否则同一 metric_id 下不同供应商的
    量纲不一致会被 write_fact 的「值不同即更正」逻辑误判成真实数值变化。
    """
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
        "INSERT INTO core.metric_source_map "
        " (metric_id, provider, provider_field, scale_factor) "
        "VALUES ('revenue_total','tushare','revenue_total',0.0001)"
    )
    conn.commit()
    writer = PointInTimeWriter(conn, ingest_run_id="r1", source="tushare")

    # 供应商原始值是元；scale_factor=0.0001 换算成本系统的万元单位。
    raw_yuan_value = 1234050000.0
    outcome = writer.write_fact(_record(raw_yuan_value, datetime(2024, 10, 28, 18, 32, tzinfo=UTC)))
    assert outcome is WriteOutcome.INSERTED

    (stored_value,) = conn.execute("SELECT value FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert abs(float(stored_value) - 123405.0) < 1e-6

    # 用同一原始值（元）再写一次：换算后与已存的万元值相同，应判定为幂等跳过，
    # 而不是因为「1234050000.0 != 123405.0」误判成一次更正。
    repeat = writer.write_fact(_record(raw_yuan_value, datetime(2024, 10, 29, 18, 32, tzinfo=UTC)))
    assert repeat is WriteOutcome.SKIPPED_IDENTICAL


# --- write_price（阶段 F：core.price_daily 至今零接入代码）------------------


def _price(
    close: float, known_at: datetime, *, trade_date: date | None = None, ref: str = "688256.SH"
) -> PriceRow:
    return PriceRow(
        entity_ref=ref,
        trade_date=trade_date or known_at.date(),
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        pre_close=close - 0.5,
        volume=1_000_000.0,
        amount=close * 1_000_000.0,
        adj_factor=1.0,
        is_suspended=False,
        known_at=known_at,
    )


@pytest.mark.db
def test_first_price_write_inserts(writer: PointInTimeWriter) -> None:
    outcome = writer.write_price(_price(50.0, datetime(2024, 10, 28, 15, 30, tzinfo=UTC)))
    assert outcome is WriteOutcome.INSERTED
    (n,) = writer.conn.execute("SELECT count(*) FROM core.price_daily").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_identical_price_rewrite_is_skipped_not_duplicated(writer: PointInTimeWriter) -> None:
    rec = _price(50.0, datetime(2024, 10, 28, 15, 30, tzinfo=UTC))
    assert writer.write_price(rec) is WriteOutcome.INSERTED
    assert writer.write_price(rec) is WriteOutcome.SKIPPED_IDENTICAL
    (n,) = writer.conn.execute("SELECT count(*) FROM core.price_daily").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_changed_close_triggers_price_correction(writer: PointInTimeWriter) -> None:
    trade_date = date(2024, 10, 28)
    first_known_at = datetime(2024, 10, 28, 15, 30, tzinfo=UTC)
    writer.write_price(_price(50.0, first_known_at, trade_date=trade_date))
    corrected_at = datetime(2024, 10, 29, 9, 0, tzinfo=UTC)
    outcome = writer.write_price(_price(51.0, corrected_at, trade_date=trade_date))
    assert outcome is WriteOutcome.CORRECTED

    rows = writer.conn.execute(
        "SELECT close, superseded_at FROM core.price_daily ORDER BY known_at"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == corrected_at
    assert rows[1][1] is None


@pytest.mark.db
def test_older_known_at_price_is_rejected(writer: PointInTimeWriter) -> None:
    trade_date = date(2024, 10, 28)
    later_known_at = datetime(2024, 10, 29, 9, 0, tzinfo=UTC)
    writer.write_price(_price(51.0, later_known_at, trade_date=trade_date))
    with pytest.raises(OutOfOrderCorrection):
        writer.write_price(
            _price(50.0, datetime(2024, 10, 28, 15, 30, tzinfo=UTC), trade_date=trade_date)
        )


@pytest.mark.db
def test_write_prices_reports_counts_per_outcome(writer: PointInTimeWriter) -> None:
    t = datetime(2024, 10, 28, 15, 30, tzinfo=UTC)
    counts = writer.write_prices([_price(50.0, t), _price(50.0, t)])
    assert counts == {WriteOutcome.INSERTED: 1, WriteOutcome.SKIPPED_IDENTICAL: 1}
