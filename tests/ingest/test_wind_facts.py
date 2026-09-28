"""Wind 财务事实入库：取数 → 原始快照 → 严格解析 → 时点写入。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind import WindFundamentalsAdapter
from ragdemo.ingest.wind_facts import ingest_wind_facts
from ragdemo.seed.loader import load_metrics, load_source_maps
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
FIXTURE = Path("tests/fixtures/wind/fundamentals_long_688041.json")
CST = timezone(timedelta(hours=8))


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息', 'listed', 'SSE', '算力', 'AI芯片', ARRAY['通用服务器CPU'],"
        "  '通用服务器CPU', '688041.SH')"
    )
    load_metrics(c, Path("db/seed/node_metric.csv"))
    load_source_maps(c, Path("db/seed/metric_source_map.csv"))
    c.commit()
    yield c
    c.close()


def _gateway(payload: Any) -> MockMcpGateway:  # noqa: ANN401
    return MockMcpGateway(
        tools=[ToolSpec("get_stock_fundamentals", "", {})],
        responses={"get_stock_fundamentals": payload},
    )


def _run(conn: psycopg.Connection, tmp_path: Path, payload: Any) -> Any:  # noqa: ANN401
    adapter = WindFundamentalsAdapter(_gateway(payload), windcodes=["688041.SH"], quarters=5)
    return ingest_wind_facts(
        conn, adapter, LocalBlobStore(tmp_path), partition_date=date(2025, 5, 1), ingest_run_id="t"
    )


@pytest.mark.db
def test_facts_are_written_with_snapshot_known_at_and_exact_yuan(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    report = _run(conn, tmp_path, json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert report.failed == {}
    assert report.written == 40  # 5 期 × 8 科目

    row = conn.execute(
        "SELECT value, unit, known_at, snapshot_id, source FROM core.fin_fact"
        " WHERE metric_id = 'revenue_total' AND period = '2024FY'"
    ).fetchone()
    assert row is not None
    value, unit, known_at, snapshot_id, source = row
    assert Decimal(value) == Decimal("9162150000")
    assert unit == "CNY" and source == "wind" and snapshot_id is not None
    assert known_at == datetime(2025, 3, 1, 23, 59, 59, tzinfo=CST)

    snap = conn.execute(
        "SELECT provider, endpoint FROM core.provider_snapshot WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()
    assert snap == ("wind", "get_stock_fundamentals")


@pytest.mark.db
def test_rerun_is_idempotent(conn: psycopg.Connection, tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _run(conn, tmp_path, payload)
    again = _run(conn, tmp_path, payload)
    assert again.written == 0 and again.skipped == 40
    count = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()
    assert count is not None and count[0] == 40


@pytest.mark.db
def test_rejected_response_writes_nothing_for_that_entity_but_keeps_the_snapshot(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    report = _run(conn, tmp_path, {"data": None, "error": {"message": "额度不足"}})
    assert set(report.failed) == {"688041.SH"}
    facts = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()
    snaps = conn.execute("SELECT count(*) FROM core.provider_snapshot").fetchone()
    assert facts is not None and facts[0] == 0
    assert snaps is not None and snaps[0] == 1, "被拒的原始响应也要留底，方便排查"


@pytest.mark.db
def test_ambiguous_cumulative_or_single_quarter_response_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """旧模板那份把单季度值标成累计的真实响应：一条都不能进库。"""
    payload = json.loads(
        Path("tests/fixtures/wind/fundamentals_ambiguous_688041.json").read_text(encoding="utf-8")
    )
    report = _run(conn, tmp_path, payload)
    assert set(report.failed) == {"688041.SH"} and "单季度" in report.failed["688041.SH"]
    facts = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()
    assert facts is not None and facts[0] == 0


@pytest.mark.db
def test_period_with_bad_disclosure_date_is_reported_not_written(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    table = payload["data"]["data"][0]
    idx = next(i for i, c in enumerate(table["columns"]) if "实际披露日期" in c["name"])
    table["rows"][3][idx] = "Q1 FY2025"
    report = _run(conn, tmp_path, payload)
    assert report.failed == {} and report.written == 32
    assert any("2024FY" in w for w in report.warnings)
