"""Dagster 资产：幂等、分区起点、回填时 known_at 落在历史区间。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.base import FactRecord
from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.ingest.assets import DAILY, fact_normalized, fin_fact_loaded
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES "
        "('CN.688256','寒武纪-U','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','688256.SH'),"
        "('CN.002049','紫光国微','listed','算力','AI芯片',ARRAY['云端训练芯片'],"
        " '云端训练芯片','002049.SZ')"
    )
    for metric in ("revenue_total", "rd_expense"):
        c.execute(
            "INSERT INTO core.node_metric (metric_id, metric_name, metric_role,"
            " frequency, source_type, definition, unit) "
            "VALUES (%s,%s,'confirming','quarterly','filing','口径','CNY')",
            (metric, metric),
        )
        c.execute(
            "INSERT INTO core.metric_source_map (metric_id, provider, provider_field) "
            "VALUES (%s,'mock',%s)",
            (metric, metric),
        )
    c.commit()
    return c


def test_partition_starts_2022_for_three_years_of_backtest() -> None:
    assert DAILY.start.date() == date(2022, 1, 1)


@pytest.mark.db
def test_fact_normalized_produces_records(conn: psycopg.Connection) -> None:
    ctx = build_asset_context(partition_key="2024-10-28")
    records = cast(list[FactRecord], fact_normalized(ctx, MockFactAdapter()))
    assert records
    assert all(r.known_at.tzinfo is not None for r in records)


@pytest.mark.db
def test_asset_is_idempotent_across_reruns(conn: psycopg.Connection) -> None:
    """同一分区重跑两次，第二次全部 SKIPPED，表里仍只有一份数据。"""
    ctx = build_asset_context(partition_key="2024-10-28")
    records = cast(list[FactRecord], fact_normalized(ctx, MockFactAdapter()))

    writer = PointInTimeWriter(conn, ingest_run_id="run-1", source="mock")
    first = cast(dict[WriteOutcome, int], fin_fact_loaded(ctx, records, writer))
    (after_first,) = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]

    writer2 = PointInTimeWriter(conn, ingest_run_id="run-2", source="mock")
    second = cast(dict[WriteOutcome, int], fin_fact_loaded(ctx, records, writer2))
    (after_second,) = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()  # type: ignore[misc]

    assert first[WriteOutcome.INSERTED] == 3
    assert second.get(WriteOutcome.INSERTED, 0) == 0
    assert second[WriteOutcome.SKIPPED_IDENTICAL] == 3
    assert after_first == after_second == 3


@pytest.mark.db
def test_backfilling_an_old_partition_keeps_known_at_historical(
    conn: psycopg.Connection,
) -> None:
    """回填 2022 年的分区，known_at 必须是 2022 年，不是今天。

    MockFactAdapter 的 period_end 同样由 partition_date 推导（见
    adapters/mock/facts.py 的 _period_end_for），所以回填老分区不会产生
    known_at 早于 period_end 的记录——fact_normalized() 在构造 FactRecord
    时就会用 ValueError 拦下这种记录，这里额外用 SQL 直接验证写进库的
    period_end 确实落在 known_at 之前，证明这条不变量在整条链路上成立，
    不只是没被异常打断。
    """
    ctx = build_asset_context(partition_key="2022-03-15")
    records = cast(list[FactRecord], fact_normalized(ctx, MockFactAdapter()))
    writer = PointInTimeWriter(conn, ingest_run_id="backfill", source="mock")
    fin_fact_loaded(ctx, records, writer)

    (known_at,) = conn.execute("SELECT min(known_at) FROM core.fin_fact").fetchone()  # type: ignore[misc]
    assert known_at.year == 2022

    (leak_count,) = conn.execute(
        "SELECT count(*) FROM core.fin_fact WHERE known_at::date < period_end"
    ).fetchone()  # type: ignore[misc]
    assert leak_count == 0
