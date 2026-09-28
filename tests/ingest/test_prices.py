"""行情三层降级（ADR-0012）：Tushare MCP → Tushare REST → Wind 日 K 线。

验的是接线后的行为：主层能用就不碰后面；主层被拒时后面的层照样交出带快照、带
真实来源的行；全部不可用时明确失败，不交出半份数据。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.ingest.prices import PriceSources
from ragdemo.ingest.writer import PointInTimeWriter
from ragdemo.sources.health import MemorySourceHealth
from ragdemo.sources.router import AllLayersFailed
from ragdemo.sources.routing import Capability
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

DAY = date(2025, 5, 7)
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
NO_PERMISSION = "tushare API 错误 [40203]: 抱歉，您没有接口(daily)访问权限"
HOURLY = "tushare API 错误 [40203]: 抱歉，您访问接口(daily)频率超限(1次/小时)"


def _json(path: str) -> Any:  # noqa: ANN401
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息', 'listed', 'SSE', '算力', 'AI芯片', ARRAY['通用服务器CPU'],"
        "  '通用服务器CPU', '688041.SH')"
    )
    c.commit()
    yield c
    c.close()


def _mcp(*, ok: bool, message: str = NO_PERMISSION) -> MockMcpGateway:
    if ok:
        rows = [
            {"ts_code": "688041.SH", "trade_date": "20250507", "close": 150.8, "vol": 126742.45}
        ]
        return MockMcpGateway(tools=[ToolSpec("daily", "", {})], responses={"daily": rows})
    return MockMcpGateway(tools=[ToolSpec("daily", "", {})], failing={"daily": message})


def _rest(body: Any, calls: list[int]) -> HttpClient:  # noqa: ANN401
    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=body)

    return HttpClient(
        "tushare-rest",
        "http://proxy.test",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _wind() -> MockMcpGateway:
    return MockMcpGateway(
        tools=[ToolSpec("get_stock_kline", "", {})],
        responses={"get_stock_kline": _json("tests/fixtures/wind/kline_day_688041_20250507.json")},
    )


def _fetch(sources: PriceSources, conn: psycopg.Connection, tmp_path: Path) -> Any:  # noqa: ANN401
    ctx = FetchContext(ingest_run_id="t", partition_date=DAY)
    return sources.route_daily(ctx, conn, LocalBlobStore(tmp_path), "688041.SH")


@pytest.mark.db
def test_primary_layer_serves_when_healthy(conn: psycopg.Connection, tmp_path: Path) -> None:
    rest_calls: list[int] = []
    sources = PriceSources(
        health=MemorySourceHealth(),
        tushare_mcp=_mcp(ok=True),
        tushare_rest=_rest({}, rest_calls),
        wind_stock=_wind(),
    )
    routed = _fetch(sources, conn, tmp_path)
    assert routed.layer == "tushare-mcp" and not routed.degraded
    assert rest_calls == [], "主层能用就不碰后面的层"
    assert [r.close for r in routed.value] == [150.8]


@pytest.mark.db
def test_rest_takes_over_when_mcp_is_refused(conn: psycopg.Connection, tmp_path: Path) -> None:
    rest_calls: list[int] = []
    sources = PriceSources(
        health=MemorySourceHealth(),
        tushare_mcp=_mcp(ok=False, message=HOURLY),
        tushare_rest=_rest(
            _json("tests/fixtures/tushare_rest/rest_daily_688041_20250507.json"), rest_calls
        ),
        wind_stock=_wind(),
    )
    routed = _fetch(sources, conn, tmp_path)
    assert routed.layer == "tushare-rest" and routed.source == "tushare"
    assert not routed.degraded, "同一账号的另一条通道，数据相同，不算降级"
    assert routed.value[0].snapshot_id is not None


@pytest.mark.db
def test_wind_kline_is_the_degraded_last_resort(conn: psycopg.Connection, tmp_path: Path) -> None:
    refused = _json("tests/fixtures/tushare_rest/rest_refused_no_permission.json")
    health = MemorySourceHealth()
    sources = PriceSources(
        health=health,
        tushare_mcp=_mcp(ok=False),
        tushare_rest=_rest(refused, []),
        wind_stock=_wind(),
    )
    routed = _fetch(sources, conn, tmp_path)
    assert routed.layer == "wind-kline" and routed.degraded
    (row,) = routed.value
    assert row.source == "wind" and row.close == 150.8 and row.snapshot_id is not None
    assert [name for name, _ in routed.skipped] == ["tushare-mcp", "tushare-rest"]
    failure = health.last_failure("tushare-rest", Capability.PRICE_DAILY)
    assert failure is not None and failure.kind.value == "permission"

    writer = PointInTimeWriter(conn, ingest_run_id="t", source="tushare")
    writer.write_prices(routed.value)
    conn.commit()
    stored = conn.execute("SELECT source, close FROM core.price_daily").fetchone()
    assert stored is not None and stored[0] == "wind", "降级写入的行挂真实来源"


@pytest.mark.db
def test_every_layer_down_fails_loudly(conn: psycopg.Connection, tmp_path: Path) -> None:
    wind_down = MockMcpGateway(
        tools=[ToolSpec("get_stock_kline", "", {})], failing={"get_stock_kline": "服务暂不可用"}
    )
    sources = PriceSources(
        health=MemorySourceHealth(), tushare_mcp=_mcp(ok=False), wind_stock=wind_down
    )
    with pytest.raises(AllLayersFailed):
        _fetch(sources, conn, tmp_path)


@pytest.mark.db
def test_unconfigured_layers_are_left_out(conn: psycopg.Connection, tmp_path: Path) -> None:
    sources = PriceSources(health=MemorySourceHealth(), tushare_mcp=_mcp(ok=False))
    with pytest.raises(AllLayersFailed) as info:
        _fetch(sources, conn, tmp_path)
    assert [name for name, _ in info.value.attempts] == ["tushare-mcp"]


def test_no_layer_configured_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="没有配置"):
        PriceSources(health=MemorySourceHealth())
