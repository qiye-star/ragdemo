"""行情第 3 层（降级，ADR-0012）：Wind `get_stock_kline` 日 K 线。

只在 Tushare 两条通道都不可用时用。它能当降级层的前提，2026-09-28 逐条实测过：

1. **复权**：默认 `aftime="0"` 是前复权（688041.SH 2025-05-06 返回 149.71，不复权收盘
   150.01）。前复权价随以后的分红变，是前视数据。`afdate`（复权基准日）当天的价格
   不被调整，所以**逐日取、`afdate` = 当天**，得到的就是当天的不复权价，与 Tushare
   `daily` 逐位相同。
2. **单位**：`VOLUME` 是股、`TURNOVER` 是元；Tushare `vol` 是手、`amount` 是千元。
   ÷100、÷1000 都能用 Decimal 精确换算（2025-05-07：12674245 股 = 126742.45 手，
   1910156347 元 = 1910156.347 千元）。响应里 `unit` 说明与此不符就整份拒绝。
3. **昨收**：单日请求拿不到，`pre_close` 留空，不编。复权因子同 Tushare `daily`
   路径，按 1.0 记（复权因子是另一个接口，目前限频）。
4. 非交易日返回空行，不是错误。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.adapters.tushare import A_SHARE_CLOSE_LOCAL, PriceRow

PROVIDER = "wind"
TOOL = "get_stock_kline"
CST = timezone(timedelta(hours=8))
_REQUIRED = ("TIME", "OPEN", "MATCH", "HIGH", "LOW", "TURNOVER", "VOLUME")
_EXPECTED_UNITS = {
    "OPEN": "元",
    "MATCH": "元",
    "HIGH": "元",
    "LOW": "元",
    "TURNOVER": "元",
    "VOLUME": "股",
}
_SHARES_PER_LOT = Decimal(100)  # Tushare vol 的单位是手
_YUAN_PER_THOUSAND = Decimal(1000)  # Tushare amount 的单位是千元


class WindKlineRejected(AdapterError):
    """K 线响应的列、单位或日期不符合预期，整份不用。"""


class WindKlineAdapter:
    provider = PROVIDER

    def __init__(self, gateway: GatewayClient) -> None:
        self._gateway = gateway

    def health(self) -> bool:
        return self._gateway.health()

    def fetch_day(self, ctx: FetchContext, windcode: str, day: date) -> RawResponse:
        ymd = day.strftime("%Y%m%d")
        args = {
            "windcode": windcode,
            "begin_date": ymd,
            "end_date": ymd,
            "afdate": ymd,  # 复权基准日 = 当天 → 当天价格不被调整
            "period": "10",  # 日 K
            "issusp": "1",  # 含停牌日
        }
        call = self._gateway.call_tool(TOOL, args)
        return replace(
            call.raw,
            provider=PROVIDER,
            endpoint=TOOL,
            params={**args, "ingest_run_id": ctx.ingest_run_id},
            payload=call.payload,
        )

    def parse_prices(self, raw: RawResponse) -> Iterator[PriceRow]:
        windcode = str(raw.params["windcode"])
        day = datetime.strptime(str(raw.params["begin_date"]), "%Y%m%d").date()
        data = _data(raw.payload)
        names = [str(c.get("name", "")) for c in data.get("columns", [])]
        missing = [n for n in _REQUIRED if n not in names]
        if missing:
            raise WindKlineRejected(f"K 线缺少列 {missing}")
        rows = data.get("rows") or []
        if not rows:
            return  # 非交易日
        units = {str(k).split()[0]: str(v) for k, v in (data.get("unit") or {}).items()}
        for column, unit in _EXPECTED_UNITS.items():
            if units.get(column) != unit:
                raise WindKlineRejected(
                    f"{column} 的单位是 {units.get(column)!r}，期望 {unit!r}——无法精确换算"
                )
        idx = {n: names.index(n) for n in _REQUIRED}
        for row in rows:
            traded = str(row[idx["TIME"]])[:10]
            if traded != day.isoformat():
                raise WindKlineRejected(f"请求 {day.isoformat()}，响应里是 {traded}")
            volume = _decimal(row[idx["VOLUME"]], "VOLUME")
            yield PriceRow(
                entity_ref=windcode,
                trade_date=day,
                open=_float(row[idx["OPEN"]], "OPEN"),
                high=_float(row[idx["HIGH"]], "HIGH"),
                low=_float(row[idx["LOW"]], "LOW"),
                close=float(_decimal(row[idx["MATCH"]], "MATCH")),
                pre_close=None,
                volume=float(volume / _SHARES_PER_LOT),
                amount=float(_decimal(row[idx["TURNOVER"]], "TURNOVER") / _YUAN_PER_THOUSAND),
                adj_factor=1.0,
                is_suspended=volume == 0,
                known_at=datetime.combine(day, A_SHARE_CLOSE_LOCAL, tzinfo=CST),
                source=PROVIDER,
            )


def _data(payload: Any) -> Mapping[str, Any]:  # noqa: ANN401
    if not isinstance(payload, Mapping) or payload.get("error") or not payload.get("data"):
        error = payload.get("error") if isinstance(payload, Mapping) else payload
        raise WindKlineRejected(f"Wind K 线返回错误或空响应：{error!r}")
    data = payload["data"]
    if not isinstance(data, Mapping):
        raise WindKlineRejected("K 线 data 不是对象")
    return data


def _decimal(cell: object, column: str) -> Decimal:
    try:
        return Decimal(str(cell))
    except InvalidOperation:
        raise WindKlineRejected(f"{column} 的值 {cell!r} 不是数字") from None


def _float(cell: object, column: str) -> float | None:
    return None if cell in (None, "") else float(_decimal(cell, column))


__all__ = ["WindKlineAdapter", "WindKlineRejected"]
