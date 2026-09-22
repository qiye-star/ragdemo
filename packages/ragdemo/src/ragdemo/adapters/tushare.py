"""Tushare Pro 适配器。

最关键的一行是 known_at 的取法：财务数据取 ann_date（公告日）当日 23:59:59，
不取 end_date（期末日）。取 end_date 会让 9 月 30 日就「知道」三季报——
这是最常见也最致命的前视偏差。见 docs/03-point-in-time.md §1.3。

行情数据的 known_at 取交易日当地收盘 15:30（A 股），不取拉取时间——
盘中不得用到当天的价格。

fetch() 产出的 payload 是「已展开的行字典列表」（与 mock 适配器一致），
不是 Tushare 原始的 fields/items 列式结构：AdapterContract 的通用契约测试
直接对 fetch() 产出的每一行调用 known_at()，如果 payload 还是未展开的
外层结构（一个 dict 而非 list），契约会把整个响应当成"一条记录"传进去，
known_at() 找不到 ann_date/trade_date 字段而抛 KeyError。parse()/parse_prices()
则单独接受未展开的原始响应（供 _raw() 直接构造的测试使用）——_rows() 对两种
形状都做了兼容，行列表原样返回，未展开的字典才做列式转行式解析。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse, require_aware
from ragdemo.adapters.http import HttpClient
from ragdemo.adapters.mcp_gateway import GatewayClient

CST = timezone(timedelta(hours=8))
A_SHARE_CLOSE_LOCAL = time(15, 30)
FACT_FIELDS = ("revenue", "rd_exp", "n_income", "total_assets", "total_hldr_eqy_exc_min_int")


@dataclass(frozen=True)
class PriceRow:
    entity_ref: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    pre_close: float | None
    volume: float | None
    amount: float | None
    adj_factor: float
    is_suspended: bool
    known_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(str(value), "%Y%m%d").date()


def _rows(payload: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Tushare 返回 fields + items 的列式结构，转成行式字典。

    若传入的已经是行式字典列表（fetch() 内部的产出形状），原样返回——
    这样 parse()/parse_prices() 既能接受 _raw() 直接构造的原始响应，
    也能接受 fetch() 已经展开过的结果，两种上游都不用改。
    """
    if isinstance(payload, list):
        return payload
    if payload.get("code") != 0:
        raise ValueError(f"Tushare 返回错误 code={payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data") or {}
    fields = data.get("fields", [])
    return [dict(zip(fields, item, strict=True)) for item in data.get("items", [])]


class TushareAdapter:
    """财务与行情。两类数据的 known_at 规则不同，因此分两个 parse 方法。"""

    provider = "tushare"

    def __init__(self, client: HttpClient | None = None, *, replay_dir: Path | None = None) -> None:
        """`client`/`replay_dir` 只服务 `fetch()`/`health()`（事实：走直连
        REST 或回放）——两者都不提供也是合法状态，供只用 `fetch_daily()` /
        `parse_prices()` / `known_at()` 的调用方使用（阶段 F：行情经 MCP
        网关取数，gateway 作为独立参数传给 `fetch_daily()`，不经这里）。
        唯一不允许的是同时提供两个：那样 `fetch()` 该走哪条路径就有歧义了。
        """
        if client is not None and replay_dir is not None:
            raise ValueError("client 与 replay_dir 不能同时提供")
        self._client = client
        self._replay_dir = replay_dir

    @classmethod
    def for_replay(cls, fixtures_dir: Path) -> TushareAdapter:
        """用录制的响应回放。契约测试与离线开发用。"""
        return cls(replay_dir=fixtures_dir)

    def health(self) -> bool:
        return True if self._replay_dir is not None else self._client is not None

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        if self._replay_dir is not None:
            for path in sorted(self._replay_dir.glob("*.json")):
                envelope = json.loads(path.read_text(encoding="utf-8"))
                rows = _rows(envelope)
                if not _looks_fact_shaped(rows):
                    # fetch()/parse() 是 FactAdapter 契约的方法，只服务事实数据；
                    # 价格是另一套形状（trade_date 而非 end_date），走 parse_prices()，
                    # 由直接构造的 RawResponse 喂入——按本计划的既定范围，价格的
                    # *写入* 推迟到 P1c。replay 目录里事实与价格两种夹具的
                    # *.json 混在一起（income_*.json / daily_*.json），这里跳过
                    # 价格文件，否则下游 fact_normalized 里无条件的
                    # `adapter.parse(raw)` 会在价格行上因缺 end_date 而 KeyError。
                    continue
                yield RawResponse(
                    provider=self.provider,
                    endpoint=f"/{path.stem}",
                    params={"partition": ctx.partition_date.isoformat()},
                    payload=rows,
                    http_status=200,
                    fetched_at=datetime.now(UTC),
                )
            return
        assert self._client is not None
        raw = self._client.get_json(
            "/income", {"period": ctx.partition_date.strftime("%Y%m%d"), **params}
        )
        yield replace(raw, payload=_rows(raw.payload))

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        """财务：公告日当日 23:59:59 CST。行情：交易日 15:30 CST。"""
        if "ann_date" in record:
            ann = _parse_yyyymmdd(record["ann_date"])
            return datetime.combine(ann, time(23, 59, 59), tzinfo=CST)
        trade = _parse_yyyymmdd(record["trade_date"])
        return datetime.combine(trade, A_SHARE_CLOSE_LOCAL, tzinfo=CST)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        for row in _rows(raw.payload):
            period_end = _parse_yyyymmdd(row["end_date"])
            known_at = self.known_at(row)
            for field_name in FACT_FIELDS:
                if row.get(field_name) is None:
                    continue
                yield FactRecord(
                    entity_ref=str(row["ts_code"]),
                    metric_field=field_name,
                    period=_period_label(period_end),
                    period_end=period_end,
                    value=float(row[field_name]),
                    unit="CNY",
                    currency="CNY",
                    valid_from=period_end - timedelta(days=89),
                    known_at=known_at,
                    source_ref=f"{row['ts_code']}:{row['end_date']}:{field_name}",
                )

    def parse_prices(self, raw: RawResponse) -> Iterator[PriceRow]:
        for row in _rows(raw.payload):
            yield PriceRow(
                entity_ref=str(row["ts_code"]),
                trade_date=_parse_yyyymmdd(row["trade_date"]),
                open=_maybe_float(row.get("open")),
                high=_maybe_float(row.get("high")),
                low=_maybe_float(row.get("low")),
                close=float(row["close"]),
                pre_close=_maybe_float(row.get("pre_close")),
                volume=_maybe_float(row.get("vol")),
                amount=_maybe_float(row.get("amount")),
                adj_factor=float(row.get("adj_factor", 1.0)),
                is_suspended=bool(row.get("vol") in (0, None)),
                known_at=self.known_at(row),
            )

    def fetch_daily(
        self, ctx: FetchContext, gateway: GatewayClient, *, ts_code: str
    ) -> Iterator[RawResponse]:
        """经 MCP 网关的 `daily` 工具取行情（阶段 F：`core.price_daily` 此前
        零接入代码）。

        网关（adapters/mcp_gateway.py，windIFinD-mcp）聚合 Tushare 官方 MCP +
        万得 + 同花顺 + AkShare + 财经新闻，是当前 Tushare 数据接入的实际
        生产路径——本仓库按既定分工只做客户端。字段形状与参数约定都经真实
        网关（a.finovadeep.com:8766）实测确认过，不是照 Tushare 文档猜的：

        1. `daily` 工具返回的 payload 直接是**展开过的行字典列表**
           （`[{"ts_code":...,"trade_date":...,"close":...}, ...]`），不是
           Tushare 官方 REST 接口那种 `{"code":0,"data":{"fields":[...],
           "items":[...]}}` 列式结构——`_rows()` 已经兼容两种形状（见模块
           docstring），这里直接把 `call.payload` 交给它，不需要额外转换。
        2. `ts_code` + `trade_date`（单日）与 `ts_code` + `start_date` +
           `end_date`（区间）两种参数形状网关都认。

        `ctx.watermark` 的含义是"这个源已经确认处理到哪一天了（含）"，不是
        "从哪天开始拉"——两者差一天，混淆会导致重跑同一天时把当天的数据
        又拉一遍。`start_date` 因此取 `watermark 的次日`：
        - 没有 watermark（第一次跑这个源）：退化为只拉 `partition_date` 当天，
          与不带增量游标时的既有行为一致。
        - watermark 次日晚于 partition_date（这一天已经处理过，典型场景是
          同一分区被重跑）：区间是空的，不必也不应该再调一次网关——直接不
          产出任何 RawResponse（F2 验收：第二次拉取的记录数应显著少于
          第一次，理想情况下是 0，因为这一天根本没有"新"区间）。
        - watermark 次日早于等于 partition_date（比如管线暂停了几天后恢复）：
          一次区间调用补齐 [watermark+1, partition_date] 这段缺口，不必
          逐日分别调用。

        没有 `adj_factor` 字段（这是 Tushare 另一个独立接口），
        `parse_prices()` 早就用 `row.get("adj_factor", 1.0)` 兜底，不需要
        在这里补。
        """
        if ctx.watermark:
            next_day = _parse_yyyymmdd(ctx.watermark) + timedelta(days=1)
            if next_day > ctx.partition_date:
                return  # 这一天已经在游标覆盖范围内，没有新区间可拉
            args: dict[str, Any] = {
                "ts_code": ts_code,
                "start_date": next_day.strftime("%Y%m%d"),
                "end_date": ctx.partition_date.strftime("%Y%m%d"),
            }
        else:
            args = {"ts_code": ts_code, "trade_date": ctx.partition_date.strftime("%Y%m%d")}

        call = gateway.call_tool("daily", args)
        yield replace(call.raw, payload=_rows(call.payload), endpoint="daily")


def _maybe_float(value: Any) -> float | None:  # noqa: ANN401
    return None if value is None else float(value)


def _looks_fact_shaped(rows: list[dict[str, Any]]) -> bool:
    """事实行必有 end_date（期末日，parse() 靠它算 period_end）；价格行没有，
    只有 trade_date。用这一个必需字段区分 replay 目录里混杂的两种夹具。"""
    return bool(rows) and all("end_date" in row for row in rows)


def _period_label(period_end: date) -> str:
    if (period_end.month, period_end.day) == (12, 31):
        return f"{period_end.year}FY"
    return f"{period_end.year}Q{(period_end.month - 1) // 3 + 1}"
