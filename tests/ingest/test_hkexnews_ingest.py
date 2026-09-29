"""港股公告全文入库（公告双源接入计划 Task 7）。结构与 `test_cninfo_ingest.py`
对巨潮层的覆盖同构——同一套 `CninfoReport`/`parse_tier`/`IndexWriter` 编排，
只是取数走 `HkexnewsAdapter`。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest

from ragdemo.adapters.hkexnews import HkexnewsAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.ingest.announcement_index import IndexWriter
from ragdemo.ingest.hkexnews_docs import HkexnewsLayerDeps, run_hkexnews_layer
from ragdemo.ingest.parse_policy import LayerReport, ParseTier
from ragdemo.parse.textin import MockDocumentParser, PageBudget
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
PREFIX_FIXTURE = (
    'cb({"more":"0","stockInfo":[{"stockId":1000015694,"code":"09988","name":"阿里巴巴"}]});'
)
ANNOUNCEMENT_ANNUAL = {
    "NEWS_ID": "1",
    "TITLE": "2025年年度报告",
    "DATE_TIME": "23/03/2026 18:00",
    "FILE_TYPE": "PDF",
    "FILE_LINK": "/listedco/listconews/sehk/2026/0323/annual.pdf",
    "STOCK_CODE": "09988",
}
ANNOUNCEMENT_NOTICE = {
    "NEWS_ID": "2",
    "TITLE": "董事会会议召开日期",
    "DATE_TIME": "01/06/2026 09:00",
    "FILE_TYPE": "PDF",
    "FILE_LINK": "/listedco/listconews/sehk/2026/0601/notice.pdf",
    "STOCK_CODE": "09988",
}


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, wind_code) VALUES"
        " ('HK.9988', '阿里巴巴集团控股有限公司', 'listed', 'HKEX', '算力', '算力服务',"
        "  ARRAY['公有云'], '公有云', '09988.HK')"
    )
    c.commit()
    yield c
    c.close()


def _query_client(*rows: Mapping[str, object]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=PREFIX_FIXTURE)
        return httpx.Response(
            200,
            json={"recordCnt": len(rows), "result": __import__("json").dumps(list(rows))},
        )

    return HttpClient(
        "hkexnews",
        "https://www1.hkexnews.hk",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _static_client(calls: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"%PDF-fake")

    return HttpClient(
        "hkexnews",
        "https://www1.hkexnews.hk",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _deps(query_client: HttpClient, calls: list[str], blob: LocalBlobStore) -> HkexnewsLayerDeps:
    return HkexnewsLayerDeps(
        adapter=HkexnewsAdapter(query_client),
        static_client=_static_client(calls),
        parser=MockDocumentParser(),
        blob=blob,
    )


def _index(conn: psycopg.Connection) -> IndexWriter:
    return IndexWriter(conn, ingest_run_id="t", now=lambda: datetime.now(UTC))


@pytest.mark.db
def test_download_false_indexes_without_downloading(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    report = run_hkexnews_layer(
        conn,
        deps=_deps(
            _query_client(ANNOUNCEMENT_ANNUAL, ANNOUNCEMENT_NOTICE), calls, LocalBlobStore(tmp_path)
        ),
        index=_index(conn),
        entity_ref="09988.HK",
        stock_code="09988",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 9, 28, tzinfo=UTC),
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=False,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.listed == 2 and report.indexed == 2 and report.downloaded == 0 and calls == []
    rows = conn.execute(
        "SELECT has_full_text FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchall()
    assert rows == [(False,), (False,)]


@pytest.mark.db
def test_downloaded_documents_use_minute_precision_known_at(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    report = run_hkexnews_layer(
        conn,
        deps=_deps(_query_client(ANNOUNCEMENT_ANNUAL), [], LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="09988.HK",
        stock_code="09988",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 9, 28, tzinfo=UTC),
        tiers=frozenset({ParseTier.SECOND}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.downloaded == 1 and report.parsed_docs == 1
    known_at = conn.execute(
        "SELECT known_at FROM core.document WHERE source = 'hkexnews'"
    ).fetchone()
    assert known_at is not None
    hkt_hour = known_at[0].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Hong_Kong")).hour
    assert hkt_hour == 18, "分钟精度的来源直接用真实发布时刻，不套用巨潮那种当日 23:59 上界"


ANNOUNCEMENT_HTML_ONLY = {
    "NEWS_ID": "3",
    "TITLE": "只有网页版的通告",
    "DATE_TIME": "02/06/2026 09:00",
    "FILE_TYPE": "HTM",
    "FILE_LINK": "/listedco/listconews/sehk/2026/0602/notice.htm",
    "STOCK_CODE": "09988",
}


def _run(
    conn: psycopg.Connection,
    deps: HkexnewsLayerDeps,
    *,
    download: bool,
) -> LayerReport:
    return run_hkexnews_layer(
        conn,
        deps=deps,
        index=_index(conn),
        entity_ref="09988.HK",
        stock_code="09988",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 9, 28, tzinfo=UTC),
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=download,
        ingest_run_id="t",
        dry_run=False,
    )


@pytest.mark.db
def test_non_pdf_announcement_is_indexed_but_never_downloaded(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    calls: list[str] = []
    deps = _deps(
        _query_client(ANNOUNCEMENT_ANNUAL, ANNOUNCEMENT_HTML_ONLY), calls, LocalBlobStore(tmp_path)
    )
    report = _run(conn, deps, download=True)
    conn.commit()
    assert report.indexed == 2 and report.no_attachment == 1 and report.downloaded == 1
    assert len(calls) == 1
    rows: dict[str, bool] = dict(
        conn.execute(
            "SELECT source_ref, has_full_text FROM core.announcement_index"
            " WHERE superseded_at IS NULL"
        ).fetchall()
    )
    assert rows == {"1": True, "3": False}


@pytest.mark.db
def test_listing_that_disagrees_with_record_cnt_fails_loudly_and_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    from ragdemo.adapters.hkexnews import HkexnewsListingIncomplete

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix.do" in request.url.path:
            return httpx.Response(200, text=PREFIX_FIXTURE)
        # 自报 5 条，只给 1 条、声称装载完毕——目录不完整。
        return httpx.Response(
            200,
            json={
                "recordCnt": 5,
                "loadedRecord": 1,
                "hasNextRow": False,
                "result": __import__("json").dumps([ANNOUNCEMENT_ANNUAL]),
            },
        )

    client = HttpClient(
        "hkexnews",
        "https://www1.hkexnews.hk",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HkexnewsListingIncomplete):
        _run(conn, _deps(client, [], LocalBlobStore(tmp_path)), download=False)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM core.announcement_index").fetchone() == (0,)
