"""巨潮资讯网适配器（adr/0013）。

关键事实：`announcementTime` 只精确到日，实测值落在北京时间当日 0 点——
这是 020_cninfo_source_registry.sql 把 time_precision 从 'second' 改成
'day' 的直接依据，这里用真实抓到的数值（1767024000000）做回归测试，
不是编一个凑整数字。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.cninfo import (
    CninfoAdapter,
    CninfoOrgIdNotFound,
    _parse_announcement_time,
)
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket

CST = timezone(timedelta(hours=8))

TOPSEARCH_688041 = [
    {
        "code": "688041",
        "orgId": "9900048365",
        "zwjc": "海光信息",
        "category": "A股",
        "delisted": "false",
    }
]

# 真实字段，2026-09-28 实测抓到（见 adr/0013"实测记录"）。
ANNOUNCEMENT_WITH_PDF = {
    "secCode": "688041",
    "secName": "海光信息",
    "orgId": "9900048365",
    "announcementId": "1224906228",
    "announcementTitle": "海光信息技术股份有限公司2025年第三次临时股东会决议公告",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906228.PDF",
    "adjunctSize": 104,
    "adjunctType": "PDF",
}

ANNOUNCEMENT_NO_ATTACHMENT = {
    "secCode": "688041",
    "secName": "海光信息",
    "orgId": "9900048365",
    "announcementId": "1224900001",
    "announcementTitle": "一条没有附件的公告（只有正文，没有 PDF）",
    "announcementTime": 1767024000000,
    "adjunctUrl": None,
    "adjunctType": None,
}


def _client(handler: Any) -> HttpClient:  # noqa: ANN401
    return HttpClient(
        "cninfo",
        "https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def test_parse_announcement_time_decodes_to_beijing_midnight() -> None:
    """回归测试：这是当初发现 time_precision 登记错误的那个真实数值。

    1767024000000 ms 解码后必须落在北京时间当日 00:00:00.000000——如果
    这条断言开始失败，说明巨潮改了这个字段的语义，020_cninfo_source_
    registry.sql 的 'day' 登记与 adapters/cninfo.py 模块 docstring 的
    "不在这里做保守上界换算"这个设计前提都要跟着重新评估。
    """
    parsed = _parse_announcement_time(1767024000000)
    local = parsed.astimezone(CST)
    assert (local.year, local.month, local.day) == (2025, 12, 30)
    assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def test_resolve_org_id_is_cached_across_calls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=TOPSEARCH_688041)

    adapter = CninfoAdapter(_client(handler))
    first = adapter.resolve_org_id("688041")
    second = adapter.resolve_org_id("688041")
    assert first == second == "9900048365"
    assert calls["n"] == 1


def test_resolve_org_id_raises_when_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    adapter = CninfoAdapter(_client(handler))
    with pytest.raises(CninfoOrgIdNotFound):
        adapter.resolve_org_id("000001")


def test_fetch_queries_both_exchange_columns_and_paginates() -> None:
    """`column` 参数（szse/sse）两个都要查一遍；命中的那个还要按 totalpages 翻页。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        seen.append(body)
        if body["column"] == "sse":
            # 查错交易所：巨潮不报错，只返回 0 条（模块顶部注释）。
            return httpx.Response(
                200, json={"announcements": [], "totalpages": 0, "totalAnnouncement": 0}
            )
        page = int(body["pageNum"])
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "announcements": [ANNOUNCEMENT_WITH_PDF],
                    "totalpages": 2,
                    "totalAnnouncement": 2,
                },
            )
        return httpx.Response(
            200,
            json={
                "announcements": [ANNOUNCEMENT_NO_ATTACHMENT],
                "totalpages": 2,
                "totalAnnouncement": 2,
            },
        )

    adapter = CninfoAdapter(_client(handler))
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime(2026, 9, 28).date())
    raws = list(
        adapter.fetch(
            ctx,
            stock_code="688041",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            until=datetime(2025, 12, 31, tzinfo=UTC),
        )
    )
    # szse 两页 + sse 一页（0 条但 totalpages=0，循环只跑一次就停）= 3 个 RawResponse。
    assert len(raws) == 3
    szse_calls = [b for b in seen if b["column"] == "szse"]
    assert [b["pageNum"] for b in szse_calls] == ["1", "2"]


def test_parse_announcements_skips_entries_without_pdf() -> None:
    raw = RawResponse(
        provider="cninfo",
        endpoint="/new/hisAnnouncement/query",
        params={},
        payload={"announcements": [ANNOUNCEMENT_WITH_PDF, ANNOUNCEMENT_NO_ATTACHMENT]},
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    refs = list(CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw))
    assert [r.announcement_id for r in refs] == ["1224906228"]


def test_pdf_path_is_absolute_from_adjunct_url() -> None:
    raw = RawResponse(
        provider="cninfo",
        endpoint="/new/hisAnnouncement/query",
        params={},
        payload={"announcements": [ANNOUNCEMENT_WITH_PDF]},
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    ref = next(CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw))
    assert ref.pdf_path == "/finalpage/2025-12-30/1224906228.PDF"


def test_fetch_full_text_downloads_from_the_static_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/finalpage/2025-12-30/1224906228.PDF"
        return httpx.Response(200, content=b"%PDF-1.4 fake bytes")

    raw = RawResponse(
        provider="cninfo",
        endpoint="",
        params={},
        payload={"announcements": [ANNOUNCEMENT_WITH_PDF]},
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    ref = next(CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw))
    static_client = HttpClient(
        "cninfo-static",
        "https://static.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )
    body = CninfoAdapter(_client(lambda r: httpx.Response(200))).fetch_full_text(ref, static_client)
    assert body == b"%PDF-1.4 fake bytes"
