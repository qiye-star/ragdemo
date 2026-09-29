"""中国货币网 CFETS 人民币汇率中间价（阶段二 Task 4）。

官方公开、免费、无需凭据（2026-09-29 实测）：

    GET https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew
        ?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&currency=USD/CNY&pageNum=1&pageSize=N

返回 `{"data": {"searchlist": ["USD/CNY"], "pageTotal": n, …}, "records":
[{"date": "2026-09-29", "values": ["6.7411"]}, …]}`。

**口径不统一**——入库前统一成"1 单位外币 = X 人民币"：
- `USD/CNY`、`EUR/CNY`、`HKD/CNY`：原样；
- `100JPY/CNY`：报价是每 100 日元，除以 100；
- `CNY/KRW`：报价是 1 元人民币折多少韩元，取倒数（12 位小数、四舍五入到偶）。

`known_at` = 该日 09:15（北京时间）：中间价每个交易日 09:15 公布，公布之前不可获知。

接口有 WAF：短时间连续请求会返回 403 HTML 页（2026-09-29 实测，冷却前持续 403），
所以取数要走限速的 `HttpClient`，并且按半年分窗、跟着 `pageTotal` 翻页，不一次要几年。
403 页不是 JSON——`HttpClient.get_json` 会把它当作解析失败抛出，不会被静默当成"没有数据"。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.http import HttpClient
from ragdemo.adapters.structured_rows import FxRow

PROVIDER = "cfets"
ENDPOINT = "/ags/ms/cm-u-bk-ccpr/CcprHisNew"
PAIRS = ("USD/CNY", "EUR/CNY", "HKD/CNY", "100JPY/CNY", "CNY/KRW")
WINDOW_DAYS = 180
PAGE_SIZE = 100
_CST = timezone(timedelta(hours=8))
_PUBLISH_TIME = time(9, 15)
_QUANT = Decimal("0.000000000001")  # 12 位小数


class CfetsResponseRejected(AdapterError):
    """响应结构不符合预期（币种对不符、数值无法解析），整份不入库。"""


def normalise_pair(pair: str, quoted: Decimal) -> tuple[str, str, Decimal]:
    """把 CFETS 的报价口径归一成 (base, quote, rate)：1 单位 base = rate 单位 quote。"""
    if pair.startswith("100"):
        base, quote = pair[3:].split("/")
        return base, quote, (quoted / 100).quantize(_QUANT, rounding=ROUND_HALF_EVEN)
    base, quote = pair.split("/")
    if base == "CNY":  # CNY/KRW：1 元折多少外币 → 取倒数得 1 外币折多少元
        return quote, base, (Decimal(1) / quoted).quantize(_QUANT, rounding=ROUND_HALF_EVEN)
    return base, quote, quoted


def known_at_of(rate_date: date) -> datetime:
    return datetime.combine(rate_date, _PUBLISH_TIME, tzinfo=_CST)


class CfetsAdapter:
    provider = PROVIDER

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def health(self) -> bool:
        return True

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        """`pair`（`PAIRS` 之一）、`start`、`end`（含）。按半年分窗、逐页取。"""
        pair = str(params["pair"])
        if pair not in PAIRS:
            raise ValueError(f"不支持的币种对 {pair!r}，可选 {PAIRS}")
        start, end = params["start"], params["end"]
        cursor = start
        while cursor <= end:
            window_end = min(cursor + timedelta(days=WINDOW_DAYS - 1), end)
            page = 1
            while True:
                raw = self._client.get_json(
                    ENDPOINT,
                    {
                        "startDate": cursor.isoformat(),
                        "endDate": window_end.isoformat(),
                        "currency": pair,
                        "pageNum": str(page),
                        "pageSize": str(PAGE_SIZE),
                    },
                )
                yield raw
                payload = raw.payload if isinstance(raw.payload, dict) else {}
                total_pages = int((payload.get("data") or {}).get("pageTotal") or 1)
                if page >= total_pages:
                    break
                page += 1
            cursor = window_end + timedelta(days=1)

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        rate_date = record if isinstance(record, date) else date.fromisoformat(str(record["date"]))
        return known_at_of(rate_date)

    def parse(self, raw: RawResponse) -> list[FxRow]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        pair = str(raw.params.get("currency", ""))
        searched = (payload.get("data") or {}).get("searchlist") or []
        if pair not in PAIRS or searched != [pair]:
            raise CfetsResponseRejected(f"币种对不符：请求 {pair!r}，响应 {searched!r}")
        rows: list[FxRow] = []
        for rec in payload.get("records") or []:
            values = rec.get("values") or []
            cell = str(values[0]).strip() if values else ""
            if cell == "":
                continue  # 该日无中间价（个别币种对休市日）
            try:
                quoted = Decimal(cell)
            except InvalidOperation:
                raise CfetsResponseRejected(
                    f"{pair} {rec.get('date')}：无法解析 {cell!r}"
                ) from None
            if quoted <= 0:
                raise CfetsResponseRejected(f"{pair} {rec.get('date')}：非正数 {cell!r}")
            rate_date = date.fromisoformat(str(rec["date"]))
            base, quote, rate = normalise_pair(pair, quoted)
            rows.append(FxRow(base, quote, rate_date, rate, known_at_of(rate_date)))
        return rows
