"""CFETS 中间价适配器：口径归一、known_at、分窗翻页（阶段二 Task 4）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.cfets import (
    WINDOW_DAYS,
    CfetsAdapter,
    CfetsResponseRejected,
    normalise_pair,
)
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket

FIXTURES = Path("tests/fixtures/cfets")


def _raw(name: str, pair: str) -> RawResponse:
    return RawResponse(
        provider="cfets",
        endpoint="/ags/ms/cm-u-bk-ccpr/CcprHisNew",
        params={"currency": pair},
        payload=json.loads((FIXTURES / name).read_text(encoding="utf-8")),
        http_status=200,
        fetched_at=datetime.now(UTC),
    )


def _client(handler: Any) -> HttpClient:  # noqa: ANN401
    return HttpClient(
        "cfets",
        "https://www.chinamoney.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _adapter() -> CfetsAdapter:
    return CfetsAdapter(_client(lambda r: httpx.Response(500)))


def test_direct_quote_is_unchanged_and_known_at_is_0915_beijing() -> None:
    rows = _adapter().parse(_raw("ccpr_usdcny.json", "USD/CNY"))
    by_date = {r.rate_date: r for r in rows}
    r = by_date[date(2026, 9, 29)]
    assert (r.base_ccy, r.quote_ccy, r.rate) == ("USD", "CNY", Decimal("6.7411"))
    assert r.known_at == datetime(2026, 9, 29, 1, 15, tzinfo=UTC)  # 09:15 +08:00


def test_weekend_gap_has_no_row() -> None:
    dates = {r.rate_date for r in _adapter().parse(_raw("ccpr_usdcny.json", "USD/CNY"))}
    assert date(2026, 9, 26) not in dates and date(2026, 9, 27) not in dates  # 周六周日


def test_100jpy_is_divided_by_100() -> None:
    rows = _adapter().parse(_raw("ccpr_jpycny.json", "100JPY/CNY"))
    r = {x.rate_date: x for x in rows}[date(2026, 9, 29)]
    assert (r.base_ccy, r.quote_ccy, r.rate) == ("JPY", "CNY", Decimal("0.042750000000"))


def test_cny_krw_is_inverted() -> None:
    rows = _adapter().parse(_raw("ccpr_cnykrw.json", "CNY/KRW"))
    r = {x.rate_date: x for x in rows}[date(2026, 9, 29)]
    assert (r.base_ccy, r.quote_ccy) == ("KRW", "CNY")
    assert r.rate == (Decimal(1) / Decimal("202.16")).quantize(Decimal("0.000000000001"))
    assert Decimal("0.00494") < r.rate < Decimal("0.00495")


def test_normalise_pair_rounding_is_half_even_to_12_places() -> None:
    assert normalise_pair("CNY/KRW", Decimal("3"))[2] == Decimal("0.333333333333")


def test_response_for_the_wrong_pair_is_rejected() -> None:
    raw = _raw("ccpr_usdcny.json", "EUR/CNY")  # 请求 EUR，响应却是 USD
    with pytest.raises(CfetsResponseRejected):
        _adapter().parse(raw)


def test_unparseable_or_nonpositive_value_is_rejected() -> None:
    raw = _raw("ccpr_usdcny.json", "USD/CNY")
    payload = json.loads(json.dumps(raw.payload))
    payload["records"][0]["values"] = ["abc"]
    with pytest.raises(CfetsResponseRejected):
        _adapter().parse(RawResponse(**{**raw.__dict__, "payload": payload}))
    payload["records"][0]["values"] = ["0"]
    with pytest.raises(CfetsResponseRejected):
        _adapter().parse(RawResponse(**{**raw.__dict__, "payload": payload}))


def test_empty_cell_is_skipped_not_an_error() -> None:
    raw = _raw("ccpr_usdcny.json", "USD/CNY")
    payload = json.loads(json.dumps(raw.payload))
    payload["records"][0]["values"] = [""]
    n = len(payload["records"])
    rows = _adapter().parse(RawResponse(**{**raw.__dict__, "payload": payload}))
    assert len(rows) == n - 1


def test_fetch_splits_long_ranges_and_follows_pages() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        seen.append(q)
        page = int(q["pageNum"])
        return httpx.Response(
            200,
            json={
                "data": {"searchlist": [q["currency"]], "pageTotal": 2},
                "records": [{"date": q["startDate"], "values": [f"6.{page}"]}],
            },
        )

    adapter = CfetsAdapter(_client(handler))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 29))
    start, end = date(2025, 1, 1), date(2025, 1, 1) + timedelta(days=WINDOW_DAYS * 2 + 9)
    raws = list(adapter.fetch(ctx, pair="USD/CNY", start=start, end=end))
    assert len(raws) == 6  # 3 个窗口 × 2 页
    windows = sorted({(q["startDate"], q["endDate"]) for q in seen})
    assert windows[0][0] == start.isoformat() and windows[-1][1] == end.isoformat()
    assert windows[1][0] == (start + timedelta(days=WINDOW_DAYS)).isoformat()  # 窗口首尾相接不重叠


def test_unsupported_pair_is_refused_before_any_request() -> None:
    adapter = CfetsAdapter(_client(lambda r: (_ for _ in ()).throw(AssertionError("不应请求"))))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 29))
    with pytest.raises(ValueError):
        list(adapter.fetch(ctx, pair="TWD/CNY", start=date(2026, 1, 1), end=date(2026, 1, 2)))
