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
        if (client is None) == (replay_dir is None):
            raise ValueError("client 与 replay_dir 必须且只能提供一个")
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
