"""行情取数的三层降级（ADR-0012）：Tushare MCP → Tushare REST → Wind 日 K 线。

每一层的 `run` 都是"取数 → 记原始快照 → 解析"：解析校验不过也能切下一层，被拒的
原始响应照样留在 provider_snapshot 里。没配置的层（环境变量没给地址）直接不参与。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.http import HttpClient
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.adapters.tushare import PriceRow, TushareAdapter, daily_args
from ragdemo.adapters.wind_kline import WindKlineAdapter
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.sources.health import SourceHealth
from ragdemo.sources.router import Layer, Routed, SourceRouter
from ragdemo.sources.routing import ROUTES, Capability
from ragdemo_core.blob import BlobStore


@dataclass
class PriceSources:
    health: SourceHealth
    tushare_mcp: GatewayClient | None = None
    tushare_rest: HttpClient | None = None
    wind_stock: GatewayClient | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not any((self.tushare_mcp, self.tushare_rest, self.wind_stock)):
            raise ValueError("没有配置任何行情来源（Tushare MCP / REST / Wind 至少一个）")

    def route_daily(
        self, ctx: FetchContext, conn: psycopg.Connection, blob: BlobStore, ts_code: str
    ) -> Routed[list[PriceRow]]:
        """按分工表逐层取 `ts_code` 的日行情。每一层都在自己的函数里"取一份、留一份快照、
        解析一份"（tests/test_snapshot_coverage.py 按函数检查这一点）。"""
        tushare = TushareAdapter()

        def tushare_mcp() -> list[PriceRow]:
            assert self.tushare_mcp is not None
            rows: list[PriceRow] = []
            for raw in tushare.fetch_daily(ctx, self.tushare_mcp, ts_code=ts_code):
                sid = record_snapshot(conn, raw, ingest_run_id=ctx.ingest_run_id, blob=blob)
                rows.extend(replace(r, snapshot_id=sid) for r in tushare.parse_prices(raw))
            return rows

        def tushare_rest() -> list[PriceRow]:
            assert self.tushare_rest is not None
            rows: list[PriceRow] = []
            for raw in tushare.fetch_daily_rest(ctx, self.tushare_rest, ts_code=ts_code):
                sid = record_snapshot(conn, raw, ingest_run_id=ctx.ingest_run_id, blob=blob)
                rows.extend(replace(r, snapshot_id=sid) for r in tushare.parse_prices(raw))
            return rows

        def wind_kline() -> list[PriceRow]:
            assert self.wind_stock is not None
            kline = WindKlineAdapter(self.wind_stock)
            rows: list[PriceRow] = []
            for day in _wind_days(ctx, ts_code):
                raw = kline.fetch_day(ctx, ts_code, day)
                sid = record_snapshot(conn, raw, ingest_run_id=ctx.ingest_run_id, blob=blob)
                rows.extend(replace(r, snapshot_id=sid) for r in kline.parse_prices(raw))
            return rows

        configured: dict[str, Callable[[], list[PriceRow]] | None] = {
            "tushare-mcp": tushare_mcp if self.tushare_mcp is not None else None,
            "tushare-rest": tushare_rest if self.tushare_rest is not None else None,
            "wind-kline": wind_kline if self.wind_stock is not None else None,
        }
        layers = [
            Layer(spec.name, spec.source, spec.degraded, run)
            for spec in ROUTES[Capability.PRICE_DAILY]
            if (run := configured[spec.name]) is not None
        ]
        return SourceRouter(self.health, clock=self.clock).run(Capability.PRICE_DAILY, layers)


def _wind_days(ctx: FetchContext, ts_code: str) -> list[date]:
    """与 Tushare 同一个取数区间，逐日列出（`afdate` = 当天才是不复权价）。"""
    args = daily_args(ctx, ts_code)
    if args is None:
        return []
    if "trade_date" in args:
        return [ctx.partition_date]
    start = datetime.strptime(str(args["start_date"]), "%Y%m%d").date()
    days = (ctx.partition_date - start).days + 1
    if days > 366:
        raise ValueError(f"降级层逐日取，区间 {days} 天过长——游标 {ctx.watermark!r} 可能出了问题")
    return [start + timedelta(days=i) for i in range(days)]
