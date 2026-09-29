"""巨潮资讯网公告入库（`ragdemo docs ingest-cninfo --yes` 的实际工作，adr/0013）。

公告列表与每份 PDF 都要留 provider_snapshot（CLAUDE.md §1.5）：列表响应内联存
jsonb；PDF 字节已经在 blob 里，快照只记 `response_ref` 指向它——与 EDGAR 全文
入库（`tests/ingest/test_edgar_ingest.py`）同一个模式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from ragdemo.adapters.announcements import NormalizedBlock
from ragdemo.adapters.cninfo import CninfoAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.ingest.announcement_index import IndexWriter
from ragdemo.ingest.cninfo_docs import (
    CninfoLayerDeps,
    ingest_cninfo_announcements,
    list_announcements,
    run_cninfo_layer,
)
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.parse.textin import MockDocumentParser, PageBudget, ParseResult
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

TOPSEARCH_688041 = [{"code": "688041", "orgId": "9900048365", "zwjc": "海光信息"}]
ANNOUNCEMENT_A = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224906228",
    "announcementTitle": "海光信息技术股份有限公司2025年第三次临时股东会决议公告",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906228.PDF",
    "adjunctType": "PDF",
}
ANNOUNCEMENT_B = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224906245",
    "announcementTitle": "北京市中伦律师事务所关于海光信息技术股份有限公司"
    "2025年第三次临时股东会的法律意见书",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906245.PDF",
    "adjunctType": "PDF",
}


@pytest.fixture
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code)"
        " VALUES ('CN.688041', '海光信息技术股份有限公司', 'listed', 'SSE', '算力',"
        " 'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片', '688041.SH')"
    )
    c.commit()
    return c


def _query_client() -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        if body["column"] == "sse":
            return httpx.Response(200, json={"announcements": [], "totalpages": 0})
        return httpx.Response(
            200,
            json={"announcements": [ANNOUNCEMENT_A, ANNOUNCEMENT_B], "totalpages": 1},
        )

    return HttpClient(
        "cninfo",
        "https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _static_client(calls: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=f"%PDF-fake {request.url.path}".encode())

    return HttpClient(
        "cninfo-static",
        "https://static.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _window() -> tuple[datetime, datetime]:
    return datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)


def test_list_announcements_does_not_touch_the_database() -> None:
    since, until = _window()
    refs = list_announcements(
        CninfoAdapter(_query_client()),
        stock_code="688041",
        since=since,
        until=until,
        ingest_run_id="t",
    )
    assert {r.announcement_id for r in refs} == {"1224906228", "1224906245"}


@pytest.mark.db
def test_list_and_every_pdf_are_snapshotted(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    calls: list[str] = []
    since, until = _window()
    written = ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client(calls),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=None,
        ingest_run_id="t",
    )
    assert len(written) == 2 and len(calls) == 2

    list_snapshots = conn.execute(
        "SELECT count(*) FROM core.provider_snapshot"
        " WHERE provider = 'cninfo' AND response IS NOT NULL"
    ).fetchone()
    assert list_snapshots is not None and list_snapshots[0] >= 1, "公告列表响应也要留底"

    rows = conn.execute(
        "SELECT d.raw_ref, d.entity_id, s.response_ref, s.provider"
        "  FROM core.document d JOIN core.provider_snapshot s USING (snapshot_id)"
        " WHERE d.source = 'cninfo'"
    ).fetchall()
    assert len(rows) == 2, "每份公告文档都必须带 snapshot_id"
    for raw_ref, entity_id, response_ref, provider in rows:
        assert entity_id == "CN.688041"
        assert provider == "cninfo-static"
        assert response_ref == raw_ref
        assert blob.get(cast(str, response_ref)).startswith(b"%PDF-fake")


@pytest.mark.db
def test_known_at_is_the_day_end_not_midnight(conn: psycopg.Connection, tmp_path: Path) -> None:
    """回归测试：真正落库的 known_at 要经过 source_registry.time_precision='day'
    的保守上界换算，不能是巨潮原始给的当日 0 点（这正是 adr/0013 发现的 bug）。
    """
    blob = LocalBlobStore(tmp_path)
    since, until = _window()
    ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client([]),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=1,
        ingest_run_id="t",
    )
    row = conn.execute(
        "SELECT known_at, publish_at FROM core.document WHERE source = 'cninfo' LIMIT 1"
    ).fetchone()
    assert row is not None
    known_at, publish_at = row
    local_publish_at = publish_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local_publish_at.hour, local_publish_at.minute) == (0, 0), (
        "原始 publish_at 就是巨潮给的午夜（北京时间）"
    )
    local_known_at = known_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local_known_at.hour, local_known_at.minute, local_known_at.second) == (23, 59, 59), (
        "known_at 必须是发布当日 23:59:59（北京时间）保守上界，不是原始午夜"
    )


@pytest.mark.db
def test_limit_caps_how_many_documents_get_written(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    blob = LocalBlobStore(tmp_path)
    since, until = _window()
    written = ingest_cninfo_announcements(
        conn,
        adapter=CninfoAdapter(_query_client()),
        static_client=_static_client([]),
        blob=blob,
        stock_code="688041",
        entity_ref="688041.SH",
        source="cninfo",
        since=since,
        until=until,
        limit=1,
        ingest_run_id="t",
    )
    assert len(written) == 1


# --- run_cninfo_layer：目录 + 选择性下载 + 按策略解析（公告双源接入计划 Task 4）------

ANNOUNCEMENT_QUARTERLY = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224906300",
    "announcementTitle": "海光信息技术股份有限公司2025年第三季度报告",
    "announcementTime": 1767024000000,
    "adjunctUrl": "finalpage/2025-12-30/1224906300.PDF",
    "adjunctType": "PDF",
}


def _query_client_with(*rows: dict[str, object]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        body = dict(httpx.QueryParams(request.content.decode()))
        if body["column"] == "sse":
            return httpx.Response(200, json={"announcements": [], "totalpages": 0})
        return httpx.Response(200, json={"announcements": list(rows), "totalpages": 1})

    return HttpClient(
        "cninfo",
        "https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _deps(
    query_client: HttpClient, static_calls: list[str], blob: LocalBlobStore
) -> CninfoLayerDeps:
    return CninfoLayerDeps(
        adapter=CninfoAdapter(query_client),
        static_client=_static_client(static_calls),
        parser=MockDocumentParser(),
        blob=blob,
    )


def _index(conn: psycopg.Connection) -> IndexWriter:
    return IndexWriter(conn, ingest_run_id="t", now=lambda: datetime.now(UTC))


@pytest.mark.db
def test_download_false_indexes_everything_but_downloads_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    since, until = _window()
    report = run_cninfo_layer(
        conn,
        deps=_deps(
            _query_client_with(ANNOUNCEMENT_A, ANNOUNCEMENT_QUARTERLY),
            calls,
            LocalBlobStore(tmp_path),
        ),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=False,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.listed == 2 and report.indexed == 2
    assert report.downloaded == 0 and calls == []
    rows = conn.execute(
        "SELECT has_full_text FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchall()
    assert rows == [(False,), (False,)]


@pytest.mark.db
def test_every_announcement_is_indexed_and_downloaded_but_only_first_tier_is_parsed(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    since, until = _window()
    report = run_cninfo_layer(
        conn,
        deps=_deps(
            _query_client_with(ANNOUNCEMENT_A, ANNOUNCEMENT_QUARTERLY),
            calls,
            LocalBlobStore(tmp_path),
        ),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.listed == 2 and report.indexed == 2 and report.downloaded == 2
    assert report.parsed_docs == 1 and report.stored_unparsed == 1

    parsed = conn.execute(
        "SELECT d.title, count(b.block_id) FROM core.document d"
        " LEFT JOIN core.doc_block b USING (doc_id) WHERE d.source = 'cninfo'"
        " GROUP BY d.doc_id, d.title ORDER BY d.title"
    ).fetchall()
    by_title: dict[str, int] = dict(parsed)
    assert by_title[str(ANNOUNCEMENT_QUARTERLY["announcementTitle"])] == 3  # 季报被解析，有块
    assert by_title[str(ANNOUNCEMENT_A["announcementTitle"])] == 0  # 决议公告未命中首批档，无块

    linked = conn.execute(
        "SELECT count(*) FROM core.announcement_index WHERE doc_id IS NOT NULL"
    ).fetchone()
    assert linked == (2,), "两份公告都下载并落库成了 core.document，目录行都要挂上 doc_id"


@pytest.mark.db
def test_budget_exhaustion_stores_the_rest_unparsed_not_dropped(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    second_quarterly = {
        **ANNOUNCEMENT_QUARTERLY,
        "announcementId": "1224906301",
        "announcementTitle": "海光信息技术股份有限公司2025年第一季度报告",
        "adjunctUrl": "finalpage/2025-12-30/1224906301.PDF",
    }
    since, until = _window()
    report = run_cninfo_layer(
        conn,
        deps=_deps(
            _query_client_with(ANNOUNCEMENT_QUARTERLY, second_quarterly),
            [],
            LocalBlobStore(tmp_path),
        ),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=2),  # MockDocumentParser 每份计 2 页，只够解析一份
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.parsed_docs == 1 and report.skipped_budget == 1
    docs = conn.execute("SELECT count(*) FROM core.document WHERE source = 'cninfo'").fetchone()
    assert docs == (2,), "预算耗尽的那份仍要落一行没有块的 document，不能凭空丢失"


@pytest.mark.db
def test_dry_run_downloads_nothing_and_estimates_pages(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    since, until = _window()
    report = run_cninfo_layer(
        conn,
        deps=_deps(
            _query_client_with(ANNOUNCEMENT_A, ANNOUNCEMENT_QUARTERLY),
            calls,
            LocalBlobStore(tmp_path),
        ),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=True,
    )
    assert report.listed == 2 and report.downloaded == 0 and calls == []
    assert sum(report.estimated_pages.values()) > 0
    assert conn.execute("SELECT count(*) FROM core.document").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM core.announcement_index").fetchone() == (0,)


@pytest.mark.db
def test_index_row_links_the_document(conn: psycopg.Connection, tmp_path: Path) -> None:
    since, until = _window()
    run_cninfo_layer(
        conn,
        deps=_deps(_query_client_with(ANNOUNCEMENT_QUARTERLY), [], LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    row = conn.execute(
        "SELECT i.doc_id, d.doc_id FROM core.announcement_index i"
        " JOIN core.document d ON d.doc_id = i.doc_id"
    ).fetchone()
    assert row is not None and row[0] == row[1]


class _ForbiddenTermParser:
    """`MockTableDescriber` 从真实表头列名拼描述——像"增持数量"这种股东增减持
    公告里的真实、合法列名会撞上 `describe.py::FORBIDDEN_PATTERNS`（"增持"）。
    这个 parser 复现该场景，验证 `run_cninfo_layer` 不会被这一份公告的校验失败
    炸穿（2026-09-29 对 688041.SH 真实公告实测发现的问题）。"""

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        blocks = [
            NormalizedBlock(0, "title", "", "股东增持计划", page=1, level=0),
            NormalizedBlock(
                1,
                "table",
                "股东增持计划",
                "| 股东名称 | 增持数量 |\n|---|---|\n| 张三 | 100000 |",
                page=1,
            ),
        ]
        return ParseResult(
            blocks=blocks, page_count=1, engine_version="mock:1", markdown="", json_ref=None,
            md_ref=None,
        )  # fmt: skip


@pytest.mark.db
def test_a_documents_own_validation_failure_does_not_crash_the_whole_batch(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    since, until = _window()
    report = run_cninfo_layer(
        conn,
        deps=CninfoLayerDeps(
            adapter=CninfoAdapter(_query_client_with(ANNOUNCEMENT_QUARTERLY)),
            static_client=_static_client([]),
            parser=_ForbiddenTermParser(),
            blob=LocalBlobStore(tmp_path),
        ),
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.validation_failed == 1
    assert report.parsed_docs == 0
    row = conn.execute(
        "SELECT d.doc_id FROM core.announcement_index i JOIN core.document d ON d.doc_id = i.doc_id"
    ).fetchone()
    assert row is not None, "校验失败的文档也该留一行 document 供目录关联，不能悄悄丢掉"
    blocks = conn.execute("SELECT count(*) FROM core.doc_block WHERE doc_id = %s", row).fetchone()
    assert blocks == (0,)


# --- 公告不遗漏：目录列全、与来源自报总数对账、补全文、不重复下载 ------------------

ANNOUNCEMENT_NO_PDF = {
    "secCode": "688041",
    "orgId": "9900048365",
    "announcementId": "1224900002",
    "announcementTitle": "海光信息技术股份有限公司关于某事项的公告",
    "announcementTime": 1767024000000,
    "adjunctUrl": None,
    "adjunctType": None,
}


def _run_layer(
    conn: psycopg.Connection,
    deps: CninfoLayerDeps,
    *,
    download: bool,
    dry_run: bool = False,
) -> object:
    since, until = _window()
    return run_cninfo_layer(
        conn,
        deps=deps,
        index=_index(conn),
        entity_ref="688041.SH",
        stock_code="688041",
        since=since,
        until=until,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=download,
        ingest_run_id="t",
        dry_run=dry_run,
    )


@pytest.mark.db
def test_announcement_without_a_pdf_is_still_indexed_and_never_downloaded(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    deps = _deps(
        _query_client_with(ANNOUNCEMENT_QUARTERLY, ANNOUNCEMENT_NO_PDF),
        calls,
        LocalBlobStore(tmp_path),
    )
    report = _run_layer(conn, deps, download=True)
    conn.commit()
    assert report.indexed == 2 and report.no_attachment == 1 and report.downloaded == 1  # type: ignore[attr-defined]
    assert len(calls) == 1  # 只下载了有 PDF 的那份
    rows: dict[str, bool] = dict(
        conn.execute(
            "SELECT source_ref, has_full_text FROM core.announcement_index"
            " WHERE superseded_at IS NULL"
        ).fetchall()
    )
    assert rows == {"1224906300": True, "1224900002": False}
    url = conn.execute(
        "SELECT url FROM core.announcement_index WHERE source_ref = '1224900002'"
    ).fetchone()
    assert url is not None and "disclosure/detail" in url[0]


@pytest.mark.db
def test_listing_that_disagrees_with_the_source_total_fails_loudly_and_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    from ragdemo.adapters.cninfo import CninfoListingIncomplete

    def handler(request: httpx.Request) -> httpx.Response:
        if "topSearch" in request.url.path:
            return httpx.Response(200, json=TOPSEARCH_688041)
        # 接口自报 5 条，只给了 1 条且声称没有更多——目录不完整。
        return httpx.Response(
            200,
            json={
                "announcements": [ANNOUNCEMENT_QUARTERLY],
                "totalpages": 1,
                "totalAnnouncement": 5,
                "hasMore": False,
            },
        )

    client = HttpClient(
        "cninfo",
        "https://www.cninfo.com.cn",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )
    deps = _deps(client, [], LocalBlobStore(tmp_path))
    with pytest.raises(CninfoListingIncomplete, match="自报 5 条"):
        _run_layer(conn, deps, download=False)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM core.announcement_index").fetchone() == (0,)


@pytest.mark.db
def test_already_downloaded_announcements_are_not_downloaded_again(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    deps = _deps(_query_client_with(ANNOUNCEMENT_QUARTERLY), calls, LocalBlobStore(tmp_path))
    _run_layer(conn, deps, download=True)
    conn.commit()
    assert len(calls) == 1
    report = _run_layer(conn, deps, download=True)
    conn.commit()
    assert len(calls) == 1, "往前回填会重叠窗口，已经下载并落了 document 的公告不该再下一遍"
    assert report.already_downloaded == 1  # type: ignore[attr-defined]


@pytest.mark.db
def test_entity_added_to_the_download_scope_later_gets_full_text_on_the_existing_row(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    deps = _deps(_query_client_with(ANNOUNCEMENT_QUARTERLY), calls, LocalBlobStore(tmp_path))
    _run_layer(conn, deps, download=False)
    conn.commit()
    assert calls == []
    _run_layer(conn, deps, download=True)
    conn.commit()
    live = conn.execute(
        "SELECT has_full_text, doc_id IS NOT NULL FROM core.announcement_index"
        " WHERE source_ref = '1224906300' AND superseded_at IS NULL"
    ).fetchall()
    assert live == [(True, True)]
