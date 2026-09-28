"""EDGAR 全文入库（`ragdemo docs ingest-edgar --yes` 的实际工作）。

申报列表与每份申报全文都要留 provider_snapshot（CLAUDE.md §1.5）：列表响应内联
存 jsonb；全文字节已经在 blob 里，快照只记 `response_ref` 指向它。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import psycopg
import pytest

from ragdemo.adapters.edgar import EdgarAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.ingest.edgar_docs import ingest_edgar_filings
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
FIXTURES = Path("tests/fixtures/edgar")
NVIDIA_CIK = 1045810


@pytest.fixture
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, edgar_cik)"
        " VALUES ('US.NVDA', 'NVIDIA', 'listed', 'NASDAQ', '算力', 'AI芯片',"
        " ARRAY['云端训练芯片'], '云端训练芯片', '1045810')"
    )
    c.commit()
    return c


def _archives(calls: list[str]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=f"<html>{request.url.path}</html>".encode())

    return HttpClient(
        provider="edgar-archives",
        base_url="https://www.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.db
def test_list_and_every_full_text_are_snapshotted(conn: psycopg.Connection, tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    calls: list[str] = []
    written = ingest_edgar_filings(
        conn,
        adapter=EdgarAdapter.for_replay(FIXTURES),
        archives_client=_archives(calls),
        blob=blob,
        cik=NVIDIA_CIK,
        source="edgar",
        limit=2,
        ingest_run_id="t",
    )
    assert len(written) == 2 and len(calls) == 2

    list_snapshots = conn.execute(
        "SELECT count(*) FROM core.provider_snapshot"
        " WHERE provider = 'edgar' AND response IS NOT NULL"
    ).fetchone()
    assert list_snapshots is not None and list_snapshots[0] >= 1, "申报列表响应也要留底"

    rows = conn.execute(
        "SELECT d.raw_ref, s.response_ref, s.provider"
        "  FROM core.document d JOIN core.provider_snapshot s USING (snapshot_id)"
    ).fetchall()
    assert len(rows) == 2, "每份申报文档都必须带 snapshot_id"
    for raw_ref, response_ref, provider in rows:
        assert provider == "edgar-archives"
        assert response_ref == raw_ref
        assert blob.get(cast(str, response_ref)).startswith(b"<html>")


def _run(conn: psycopg.Connection, blob: LocalBlobStore, *, limit: int) -> object:
    return ingest_edgar_filings(
        conn,
        adapter=EdgarAdapter.for_replay(FIXTURES),
        archives_client=_archives([]),
        blob=blob,
        cik=NVIDIA_CIK,
        source="edgar",
        limit=limit,
        ingest_run_id="t",
    )


def _count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return cast(int, row[0])


@pytest.mark.db
def test_rerun_skips_existing_documents_but_still_records_what_was_fetched(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """重跑时文档已存在会被跳过，但这次确实又拉了一遍全文——那次拉取本身要留底。"""
    blob = LocalBlobStore(tmp_path)
    _run(conn, blob, limit=1)
    before = _count(conn, "SELECT count(*) FROM core.provider_snapshot")
    _run(conn, blob, limit=1)
    assert _count(conn, "SELECT count(*) FROM core.document") == 1
    assert _count(conn, "SELECT count(*) FROM core.provider_snapshot") > before
