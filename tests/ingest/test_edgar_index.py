"""EDGAR 接入公告目录（公告双源接入计划 Task 8）：CIK 解析 + 目录写入，复用既有
`adapters/edgar.py` / `ingest/edgar_docs.py`，不重新实现取数与全文抓取。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest

from ragdemo.adapters.edgar import EdgarAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.ingest.announcement_index import IndexWriter
from ragdemo.ingest.edgar_index import EdgarLayerDeps, resolve_cik, run_edgar_layer
from ragdemo.ingest.parse_policy import ParseTier
from ragdemo.parse.textin import MockDocumentParser, PageBudget
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
TICKERS_FIXTURE = Path("tests/fixtures/edgar_cik/company_tickers.json")
SUBMISSIONS_FIXTURE = Path("tests/fixtures/edgar/submissions_0001045810.json")


def _tickers_client(body: bytes | None = None) -> HttpClient:
    payload = body if body is not None else TICKERS_FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    return HttpClient(
        "edgar",
        "https://www.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def test_resolve_cik_finds_known_tickers() -> None:
    client = _tickers_client()
    assert resolve_cik(client, ticker="NVDA") == 1045810
    assert resolve_cik(client, ticker="BABA") == 1577552
    assert resolve_cik(client, ticker="TSM") == 1046179
    assert resolve_cik(client, ticker="ASX") == 1122411
    assert resolve_cik(client, ticker="BIDU") == 1329099
    assert resolve_cik(client, ticker="KC") == 1795589


def test_resolve_cik_returns_none_for_unknown_ticker() -> None:
    assert resolve_cik(_tickers_client(), ticker="NOSUCHTICKER") is None


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, edgar_cik) VALUES"
        " ('US.NVDA', 'NVIDIA Corporation', 'listed', 'NASDAQ', '算力', 'AI芯片',"
        "  ARRAY['云端训练芯片'], '云端训练芯片', '1045810')"
    )
    c.commit()
    yield c
    c.close()


def _submissions_client() -> HttpClient:
    body = SUBMISSIONS_FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return HttpClient(
        "edgar",
        "https://data.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _archives_client(calls: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # 内容按路径区分，不同申报不能撞出同一个 content_hash（否则会被
        # DocumentWriter 的去重逻辑当成同一份文档，掩盖测试想验证的行为）。
        return httpx.Response(200, content=f"<html>{request.url.path}</html>".encode())

    return HttpClient(
        "edgar-archives",
        "https://www.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _deps(calls: list[str], blob: LocalBlobStore) -> EdgarLayerDeps:
    return EdgarLayerDeps(
        adapter=EdgarAdapter(client=_submissions_client()),
        archives_client=_archives_client(calls),
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
    report = run_edgar_layer(
        conn,
        deps=_deps(calls, LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="1045810",
        cik=1045810,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=False,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.listed > 0 and report.downloaded == 0 and calls == []
    rows = conn.execute(
        "SELECT has_full_text FROM core.announcement_index WHERE superseded_at IS NULL"
    ).fetchall()
    assert rows and all(r == (False,) for r in rows)


@pytest.mark.db
def test_known_at_is_the_real_acceptance_time_no_conservative_bound(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    run_edgar_layer(
        conn,
        deps=_deps([], LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="1045810",
        cik=1045810,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=False,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    known_ats = conn.execute("SELECT known_at FROM core.announcement_index").fetchall()
    assert all(k[0].second != 59 or k[0].microsecond != 999999 for k in known_ats), (
        "EDGAR 是秒级真实受理时刻，不该被套上巨潮那种当日 23:59:59.999999 上界"
    )


@pytest.mark.db
def test_form_type_drives_the_parse_tier(conn: psycopg.Connection, tmp_path: Path) -> None:
    """夹具里的 10-Q/8-K 都是 FIRST 档；只请求 SECOND 档时两份都只落无块行。"""
    calls: list[str] = []
    report = run_edgar_layer(
        conn,
        deps=_deps(calls, LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="1045810",
        cik=1045810,
        tiers=frozenset({ParseTier.SECOND}),  # 10-Q/8-K 都是 FIRST，不在这个集合里
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=False,
    )
    conn.commit()
    assert report.downloaded == 2 and report.parsed_docs == 0 and report.stored_unparsed == 2
    forms = {
        r[0]
        for r in conn.execute("SELECT title FROM core.document WHERE source = 'edgar'").fetchall()
    }
    assert len(forms) == 2 and any("10-Q" in f for f in forms) and any("8-K" in f for f in forms)


@pytest.mark.db
def test_dry_run_touches_neither_index_nor_document(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    report = run_edgar_layer(
        conn,
        deps=_deps([], LocalBlobStore(tmp_path)),
        index=_index(conn),
        entity_ref="1045810",
        cik=1045810,
        tiers=frozenset({ParseTier.FIRST}),
        budget=PageBudget(remaining=1000),
        download=True,
        ingest_run_id="t",
        dry_run=True,
    )
    assert report.listed > 0 and sum(report.estimated_pages.values()) > 0
    assert conn.execute("SELECT count(*) FROM core.announcement_index").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM core.document").fetchone() == (0,)
