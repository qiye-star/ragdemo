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


def test_fetch_paginates_and_only_tries_the_second_column_when_the_first_is_empty() -> None:
    """先试 szse 并翻页；szse 有结果就不再试 sse。"""
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
    # szse 两页；szse 已有结果，不再试 sse（见 fetch 的 docstring）。
    assert len(raws) == 2
    assert [b["pageNum"] for b in seen if b["column"] == "szse"] == ["1", "2"]
    assert not [b for b in seen if b["column"] == "sse"]


def test_parse_announcements_lists_entries_without_pdf_but_marks_them_not_downloadable() -> None:
    """公告目录必须列全：没有 PDF 附件的公告照样是一份公告，只是拿不到可解析原文。
    以前这里直接跳过，等于目录里凭空少一条（2026-09-29 用真实接口核对总数时发现）。"""
    raw = RawResponse(
        provider="cninfo",
        endpoint="/new/hisAnnouncement/query",
        params={},
        payload={"announcements": [ANNOUNCEMENT_WITH_PDF, ANNOUNCEMENT_NO_ATTACHMENT]},
        http_status=200,
        fetched_at=datetime.now(UTC),
    )
    refs = list(CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw))
    assert [r.announcement_id for r in refs] == ["1224906228", "1224900001"]
    assert [r.downloadable for r in refs] == [True, False]


@pytest.mark.parametrize("adjunct_type", ["PDF", ".PDF", ".pdf", "pdf"])
def test_adjunct_type_is_normalised_before_deciding_downloadable(adjunct_type: str) -> None:
    """近期公告的 adjunctType 是 ".PDF"（带点）——`!= "PDF"` 会把它们当成无附件丢掉。"""
    row = {**ANNOUNCEMENT_WITH_PDF, "adjunctType": adjunct_type}
    raw = RawResponse(
        provider="cninfo", endpoint="", params={}, payload={"announcements": [row]},
        http_status=200, fetched_at=datetime.now(UTC),
    )  # fmt: skip
    (ref,) = CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw)
    assert ref.downloadable


def test_non_pdf_attachment_is_listed_but_not_downloadable() -> None:
    row = {**ANNOUNCEMENT_WITH_PDF, "adjunctType": "DOC", "adjunctUrl": "finalpage/x/1.DOC"}
    raw = RawResponse(
        provider="cninfo", endpoint="", params={}, payload={"announcements": [row]},
        http_status=200, fetched_at=datetime.now(UTC),
    )  # fmt: skip
    (ref,) = CninfoAdapter(_client(lambda r: httpx.Response(200))).parse_announcements(raw)
    assert not ref.downloadable


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


def _paged_handler(total: int, page_size: int, floor_totalpages: bool) -> Any:  # noqa: ANN401
    """模拟巨潮真实行为：`totalpages` 向下取整（433 条→14 页而不是 15），
    最后一个不满页只能靠 `hasMore` 判断。"""
    ids = [str(1000 + i) for i in range(total)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        page = int(body["pageNum"])
        chunk = ids[(page - 1) * page_size : page * page_size]
        rows = [{**ANNOUNCEMENT_WITH_PDF, "announcementId": i} for i in chunk]
        pages = total // page_size if floor_totalpages else -(-total // page_size)
        return httpx.Response(
            200,
            json={
                "announcements": rows,
                "totalpages": pages,
                "totalAnnouncement": total,
                "hasMore": page * page_size < total,
            },
        )

    return handler


def test_fetch_retrieves_the_last_partial_page_even_when_totalpages_is_floored() -> None:
    """回归：`totalpages` 是向下取整的，旧代码翻到 totalpages 就停，每家实体最后
    一个不满页（0–29 条）被系统性丢掉——真实接口 totalAnnouncement=433 只拿到 420，
    抽查的 8 家实体入库条数全是 30 的整数倍。"""
    adapter = CninfoAdapter(_client(_paged_handler(total=433, page_size=30, floor_totalpages=True)))
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime(2026, 9, 28).date())
    raws = list(
        adapter.fetch(
            ctx,
            stock_code="688041",
            since=datetime(2023, 1, 1, tzinfo=UTC),
            until=datetime(2025, 12, 31, tzinfo=UTC),
        )
    )
    ids = {r.announcement_id for raw in raws for r in adapter.parse_announcements(raw)}
    assert len(ids) == 433
    assert adapter.expected_total(raws) == 433


def test_second_column_is_only_queried_when_the_first_returned_nothing() -> None:
    """真实接口对 stock 已定的查询不看 column，沪深两个 column 返回同样的全量结果——
    两个都翻一遍等于对没有 SLA 的站点白白翻倍请求。第一个 column 有结果就不再试第二个。"""
    columns: list[str] = []
    inner = _paged_handler(total=10, page_size=30, floor_totalpages=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if "hisAnnouncement" in request.url.path:
            columns.append(dict(httpx.QueryParams(request.content.decode()))["column"])
        return inner(request)  # type: ignore[no-any-return]

    adapter = CninfoAdapter(_client(handler))
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime(2026, 9, 28).date())
    list(
        adapter.fetch(
            ctx,
            stock_code="688041",
            since=datetime(2025, 1, 1, tzinfo=UTC),
            until=datetime(2025, 12, 31, tzinfo=UTC),
        )
    )
    assert set(columns) == {"szse"}


def test_expected_total_is_none_when_the_response_has_no_total() -> None:
    raw = RawResponse(
        provider="cninfo", endpoint="", params={}, payload={"announcements": []},
        http_status=200, fetched_at=datetime.now(UTC),
    )  # fmt: skip
    assert CninfoAdapter(_client(lambda r: httpx.Response(200))).expected_total([raw]) is None
