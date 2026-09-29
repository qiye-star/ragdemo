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
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


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


def test_non_pdf_entries_are_listed_but_not_downloadable() -> None:
    """没有 PDF 的条目（HTM、无附件）照样是一份公告、必须进目录——2026-09-29 实测
    商汤 132 条只入 130，多家有类型为空或 HTM 的条目被过滤掉。"""
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
    fixture["titlesearch_recordCnt"] = len(fixture["titlesearch_result"])
    adapter = HkexnewsAdapter(_client(_handler_from(fixture)))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raws = list(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 9, 28, tzinfo=UTC),
        )
    )
    refs = {r.news_id: r for raw in raws for r in adapter.parse_announcements(raw)}
    assert "999" in refs and not refs["999"].downloadable
    assert all(r.downloadable for k, r in refs.items() if k != "999")


def _handler_from(fixture: dict[str, Any]) -> Any:  # noqa: ANN401
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


def _capped_handler(total: int, cap: int) -> Any:  # noqa: ANN401
    """模拟披露易的 rowRange 上限：一个窗口内公告超过 `cap` 条时只装载前 `cap` 条，
    `recordCnt` 仍是真实总数，`hasNextRow=True`。公告均匀分布在 2023-01-01 起每天一条。"""
    base = _fixture()
    template = base["titlesearch_result"][0]
    start = date(2023, 1, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=base["prefix"])
        q = dict(request.url.params)
        lo = datetime.strptime(q["fromDate"], "%Y%m%d").date()
        hi = datetime.strptime(q["toDate"], "%Y%m%d").date()
        days = [start + timedelta(days=i) for i in range(total)]
        inside = [d for d in days if lo <= d <= hi]
        rows = [
            {**template, "NEWS_ID": f"n{d.isoformat()}", "DATE_TIME": d.strftime("%d/%m/%Y 09:00")}
            for d in inside[:cap]
        ]
        return httpx.Response(
            200,
            json={
                "recordCnt": len(inside),
                "loadedRecord": len(rows),
                "hasNextRow": len(inside) > cap,
                "result": json.dumps(rows, ensure_ascii=False),
            },
        )

    return handler


def test_window_over_the_row_cap_is_split_until_every_announcement_is_listed() -> None:
    """回归：腾讯 3 年窗口 recordCnt=535 只装载 500（hasNextRow=True），旧代码一次查询
    就交差，超出上限的公告静默丢失。"""
    adapter = HkexnewsAdapter(_client(_capped_handler(total=535, cap=500)))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raws = list(
        adapter.fetch(
            ctx,
            stock_code="09988",
            since=datetime(2023, 1, 1, tzinfo=UTC),
            until=datetime(2025, 12, 31, tzinfo=UTC),
        )
    )
    ids = {r.news_id for raw in raws for r in adapter.parse_announcements(raw)}
    assert len(ids) == 535
    assert adapter.expected_total(raws) == 535
    assert any(raw.params.get("truncated") for raw in raws), "被截断的那次响应照样留下（快照）"


def test_a_single_day_over_the_cap_fails_loudly() -> None:
    from ragdemo.adapters.hkexnews import HkexnewsListingIncomplete

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=_fixture()["prefix"])
        row = _fixture()["titlesearch_result"][0]
        return httpx.Response(
            200,
            json={
                "recordCnt": 900,
                "loadedRecord": 500,
                "hasNextRow": True,
                "result": json.dumps([row] * 500, ensure_ascii=False),
            },
        )

    adapter = HkexnewsAdapter(_client(handler))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    with pytest.raises(HkexnewsListingIncomplete, match="一天内"):
        list(
            adapter.fetch(
                ctx,
                stock_code="09988",
                since=datetime(2026, 1, 1, tzinfo=UTC),
                until=datetime(2026, 1, 3, tzinfo=UTC),
            )
        )
