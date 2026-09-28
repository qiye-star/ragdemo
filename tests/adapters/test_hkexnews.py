"""港交所披露易适配器（公告双源接入计划 Task 7）：港股公告全文，单层——披露易本身
就是交易所官方渠道，不需要也不该降级。夹具是 2026-09-28 对阿里巴巴（09988.HK）
的真实响应。

关键事实（与巨潮的对比）：
- `DATE_TIME` 是分钟精度（"23/09/2026 06:05"，DD/MM/YYYY HH:MM，香港时间），
  不像巨潮只到日——不需要"当日 23:59"这层保守上界换算。
- 每条结果自带 `FILE_LINK`（PDF 直链），不像巨潮要判断"有没有附件"。
- 双柜台股票（如阿里巴巴-W／-WR）的 `STOCK_CODE` 是用 `<br/>` 拼接的多个代码。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.hkexnews import HkexnewsAdapter, HkexnewsStockIdNotFound
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket

FIXTURE = Path("tests/fixtures/hkexnews/announcements_09988.json")
HKT = timezone(timedelta(hours=8))


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _client(handler: Any) -> HttpClient:  # noqa: ANN401
    return HttpClient(
        "hkexnews",
        "https://www1.hkexnews.hk",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _handler_from_fixture() -> Any:  # noqa: ANN401
    fixture = _fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=fixture["prefix"])
        return httpx.Response(
            200,
            json={
                "recordCnt": fixture["titlesearch_recordCnt"],
                "result": json.dumps(fixture["titlesearch_result"], ensure_ascii=False),
            },
        )

    return handler


def test_resolve_stock_id_is_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=_fixture()["prefix"])

    adapter = HkexnewsAdapter(_client(handler))
    first = adapter.resolve_stock_id("09988")
    second = adapter.resolve_stock_id("09988")
    assert first == second == "1000015694"
    assert len(calls) == 1


def test_resolve_stock_id_raises_when_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='cb({"more":"0","stockInfo":[]});')

    with pytest.raises(HkexnewsStockIdNotFound):
        HkexnewsAdapter(_client(handler)).resolve_stock_id("00000")


def test_fetch_covers_the_window_in_one_call() -> None:
    adapter = HkexnewsAdapter(_client(_handler_from_fixture()))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raws = list(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    assert len(raws) == 1
    refs = list(adapter.parse_announcements(raws[0]))
    assert len(refs) == 8
    assert refs[0].stock_code == "09988"


def test_known_at_is_minute_precision_hong_kong_time_not_a_conservative_bound() -> None:
    adapter = HkexnewsAdapter(_client(_handler_from_fixture()))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = next(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    ref = next(adapter.parse_announcements(raw))
    assert ref.publish_at == datetime(2026, 9, 23, 6, 5, tzinfo=HKT)


def test_pdf_path_is_absolute_from_file_link() -> None:
    adapter = HkexnewsAdapter(_client(_handler_from_fixture()))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = next(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    ref = next(adapter.parse_announcements(raw))
    assert ref.pdf_path == "/listedco/listconews/sehk/2026/0923/2026092300002_c.pdf"


def test_fetch_full_text_downloads_the_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/listedco/listconews/sehk/2026/0923/2026092300002_c.pdf"
        return httpx.Response(200, content=b"%PDF-fake")

    adapter = HkexnewsAdapter(_client(_handler_from_fixture()))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = next(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    ref = next(adapter.parse_announcements(raw))
    body = adapter.fetch_full_text(ref, _client(handler))
    assert body == b"%PDF-fake"


def test_non_pdf_entries_are_skipped() -> None:
    fixture = _fixture()
    fixture["titlesearch_result"].append(
        {
            "NEWS_ID": "999",
            "TITLE": "只有网页版没有 PDF 的通告",
            "DATE_TIME": "01/01/2026 09:00",
            "FILE_TYPE": "",
            "FILE_LINK": "",
            "STOCK_CODE": "09988",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=fixture["prefix"])
        return httpx.Response(
            200,
            json={
                "recordCnt": fixture["titlesearch_recordCnt"],
                "result": json.dumps(fixture["titlesearch_result"], ensure_ascii=False),
            },
        )

    adapter = HkexnewsAdapter(_client(handler))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = next(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    refs = list(adapter.parse_announcements(raw))
    assert all(r.news_id != "999" for r in refs)
