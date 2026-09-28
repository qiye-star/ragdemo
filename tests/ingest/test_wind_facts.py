"""Wind 财务事实入库：取数 → 原始快照 → 严格解析 → 时点写入。

Wind 不可用时不跨源补（ADR-0012）：记入 core.ingest_backlog，下次触发先补。
"""

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
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.health import MemorySourceHealth
from ragdemo.sources.routing import Capability
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
FIXTURE = Path("tests/fixtures/wind/fundamentals_long_688041.json")
CST = timezone(timedelta(hours=8))


@pytest.fixture
def state(temp_db: str, conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """冷却与待补清单用的 autocommit 连接（依赖 conn，保证迁移先跑完）。"""
    c = psycopg.connect(temp_db, autocommit=True)
    yield c
    c.close()


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


def _run(
    conn: psycopg.Connection,
    tmp_path: Path,
    payload: Any,  # noqa: ANN401
    *,
    gateway: Any = None,  # noqa: ANN401
    health: Any = None,  # noqa: ANN401
    backlog: Backlog | None = None,
    windcodes: tuple[str, ...] = ("688041.SH",),
    on: date = date(2025, 5, 1),
) -> Any:  # noqa: ANN401
    adapter = WindFundamentalsAdapter(gateway or _gateway(payload), windcodes=list(windcodes))
    return ingest_wind_facts(
        conn,
        adapter,
        LocalBlobStore(tmp_path),
        windcodes=windcodes,
        partition_date=on,
        ingest_run_id="t",
        quarters=5,
        health=health or MemorySourceHealth(),
        backlog=backlog,
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


@pytest.mark.db
def test_wind_down_puts_every_entity_on_the_backlog_and_writes_nothing(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.000977', '浪潮信息', 'listed', 'SZSE', '算力', '服务器', ARRAY['AI服务器'],"
        "  'AI服务器', '000977.SZ')"
    )
    conn.commit()
    down = MockMcpGateway(
        tools=[ToolSpec("get_stock_fundamentals", "", {})],
        failing={"get_stock_fundamentals": "服务暂不可用"},
    )
    health, backlog = MemorySourceHealth(), Backlog(state)
    report = _run(
        conn,
        tmp_path,
        None,
        gateway=down,
        health=health,
        backlog=backlog,
        windcodes=("688041.SH", "000977.SZ"),
    )
    assert report.written == 0 and set(report.backlogged) == {"688041.SH", "000977.SZ"}
    assert len(down.calls) == 1, "第一次失败后 Wind 进入冷却，后面的实体不再白打一次"
    items = {i.entity_ref: i for i in backlog.open_items(Capability.FINANCIALS)}
    assert items["688041.SH"].since == date(2024, 3, 31)  # 5 个季度的第一个季末
    assert items["688041.SH"].failure_kind == "unavailable"
    facts = conn.execute("SELECT count(*) FROM core.fin_fact").fetchone()
    assert facts is not None and facts[0] == 0


@pytest.mark.db
def test_next_run_fills_the_backlog_and_resolves_it(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    backlog = Backlog(state)
    down = MockMcpGateway(
        tools=[ToolSpec("get_stock_fundamentals", "", {})],
        failing={"get_stock_fundamentals": "服务暂不可用"},
    )
    _run(conn, tmp_path, None, gateway=down, backlog=backlog)

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report = _run(conn, tmp_path, payload, backlog=backlog, windcodes=())
    assert report.written == 40, "没点名的实体也要补：待补清单自己带着它"
    assert backlog.open_items(Capability.FINANCIALS) == []


@pytest.mark.db
def test_backlog_widens_the_request_back_to_the_oldest_gap(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    backlog = Backlog(state)
    down = MockMcpGateway(
        tools=[ToolSpec("get_stock_fundamentals", "", {})],
        failing={"get_stock_fundamentals": "服务暂不可用"},
    )
    _run(conn, tmp_path, None, gateway=down, backlog=backlog, on=date(2025, 5, 1))
    ok = _gateway(json.loads(FIXTURE.read_text(encoding="utf-8")))
    _run(conn, tmp_path, None, gateway=ok, backlog=backlog, on=date(2026, 5, 1))
    (question,) = [str(args["question"]) for _, args in ok.calls]
    assert "20240331" in question, "缺口从 2024Q1 开始，新一次请求要覆盖到它"


@pytest.mark.db
def test_rejected_response_is_backlogged_too(
    conn: psycopg.Connection, state: psycopg.Connection, tmp_path: Path
) -> None:
    backlog = Backlog(state)
    ambiguous = json.loads(
        Path("tests/fixtures/wind/fundamentals_ambiguous_688041.json").read_text(encoding="utf-8")
    )
    report = _run(conn, tmp_path, ambiguous, backlog=backlog)
    assert set(report.failed) == {"688041.SH"}
    (item,) = backlog.open_items(Capability.FINANCIALS)
    assert item.failure_kind == "rejected"
