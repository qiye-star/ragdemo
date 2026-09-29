"""行情区间回填：一次区间调用取回整段、幂等、不碰全局游标、失败不中断整批。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.ingest.price_backfill import backfill_prices, tushare_codes
from ragdemo.ingest.prices import PriceSources
from ragdemo.sources.health import MemorySourceHealth
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    for eid, code in (("CN.688041", "688041.SH"), ("CN.000977", "000977.SZ")):
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
            " l2_segment, l3_node, primary_node, tushare_code) VALUES"
            " (%s, %s, 'listed', 'SSE', '算力', 'AI芯片', ARRAY['x'], 'x', %s)",
            (eid, eid, code),
        )
    c.commit()
    yield c
    c.close()


class _RecordingGateway(MockMcpGateway):
    """按 ts_code 返回不同的行，并记录每次调用的参数。"""

    def __init__(self, rows: dict[str, list[dict[str, Any]]], failing_codes: set[str]) -> None:
        super().__init__(tools=[ToolSpec("daily", "", {})])
        self._rows = rows
        self._failing_codes = failing_codes

    def call_tool(self, name: str, arguments: Any) -> Any:  # noqa: ANN401
        code = str(arguments["ts_code"])
        if code in self._failing_codes:
            self._responses = {}
            self._failing = {"daily": "tushare API 错误 [40203]: 无权限"}
        else:
            self._failing = {}
            self._responses = {"daily": self._rows[code]}
        return super().call_tool(name, arguments)


def _rows(code: str) -> list[dict[str, Any]]:
    return [
        {"ts_code": code, "trade_date": d, "close": c, "vol": 1000.0}
        for d, c in (("20240102", 10.5), ("20240103", 10.7), ("20240104", 10.6))
    ]


def _sources(failing: set[str] | None = None) -> tuple[PriceSources, _RecordingGateway]:
    gw = _RecordingGateway(
        {"688041.SH": _rows("688041.SH"), "000977.SZ": _rows("000977.SZ")}, failing or set()
    )
    return PriceSources(health=MemorySourceHealth(), tushare_mcp=gw), gw


def _n(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_backfill_uses_one_range_call_per_entity_and_writes_every_day(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    sources, gw = _sources()
    report = backfill_prices(
        conn,
        sources,
        LocalBlobStore(tmp_path),
        ts_codes=tushare_codes(conn),
        since=date(2024, 1, 2),
        until=date(2024, 1, 4),
        ingest_run_id="t",
    )
    assert report.entities == 2 and report.inserted == 6 and not report.failed
    args = [a for _, a in gw.calls]
    assert [a["start_date"] for a in args] == ["20240102", "20240102"]
    assert [a["end_date"] for a in args] == ["20240104", "20240104"]
    assert _n(conn, "core.price_daily") == 6
    assert _n(conn, "core.provider_snapshot") == 2  # 每次真实调用都留了原始响应


@pytest.mark.db
def test_rerun_is_idempotent_and_the_global_watermark_is_untouched(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    sources, _ = _sources()
    kwargs: dict[str, Any] = dict(
        blob=LocalBlobStore(tmp_path),
        ts_codes=tushare_codes(conn),
        since=date(2024, 1, 2),
        until=date(2024, 1, 4),
        ingest_run_id="t",
    )
    backfill_prices(conn, sources, **kwargs)
    again = backfill_prices(conn, sources, **kwargs)
    assert again.inserted == 0 and again.skipped == 6
    assert _n(conn, "core.price_daily") == 6
    assert _n(conn, "core.ingest_watermark") == 0  # 不推进 Dagster 共用的 tushare 游标


@pytest.mark.db
def test_a_refusal_fails_that_entity_but_earlier_entities_stay_in(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """按代码排序 000977.SZ 在前：它成功入库；688041.SH 被拒，记入 failed，不回滚前者。"""
    sources, _ = _sources(failing={"688041.SH"})
    report = backfill_prices(
        conn,
        sources,
        LocalBlobStore(tmp_path),
        ts_codes=tushare_codes(conn),
        since=date(2024, 1, 2),
        until=date(2024, 1, 4),
        ingest_run_id="t",
    )
    assert report.entities == 1 and report.inserted == 3
    assert list(report.failed) == ["688041.SH"]
    assert _n(conn, "core.price_daily") == 3


@pytest.mark.db
def test_after_a_refusal_the_layer_is_cooled_so_the_rest_fail_fast_instead_of_hammering(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """无权限 / 限频是账号级的：第一个被拒后这一层进入冷却，其余实体直接记失败（"冷却中"），
    不再逐个去砸供应商；冷却过后重跑即可（写入幂等）。"""
    sources, gw = _sources(failing={"000977.SZ"})  # 排在最前
    report = backfill_prices(
        conn,
        sources,
        LocalBlobStore(tmp_path),
        ts_codes=tushare_codes(conn),
        since=date(2024, 1, 2),
        until=date(2024, 1, 4),
        ingest_run_id="t",
    )
    assert report.entities == 0 and set(report.failed) == {"000977.SZ", "688041.SH"}
    assert len(gw.calls) == 1  # 第二个实体根本没有发起调用
    assert _n(conn, "core.price_daily") == 0


@pytest.mark.db
def test_inverted_range_is_refused(conn: psycopg.Connection, tmp_path: Path) -> None:
    sources, _ = _sources()
    with pytest.raises(ValueError):
        backfill_prices(
            conn,
            sources,
            LocalBlobStore(tmp_path),
            ts_codes=["688041.SH"],
            since=date(2024, 2, 1),
            until=date(2024, 1, 1),
            ingest_run_id="t",
        )


@pytest.mark.db
def test_tushare_codes_filters_by_ref(conn: psycopg.Connection) -> None:
    assert tushare_codes(conn) == ["000977.SZ", "688041.SH"]
    assert tushare_codes(conn, ["688041.SH"]) == ["688041.SH"]
