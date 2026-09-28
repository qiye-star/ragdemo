"""EDGAR 申报全文入库（路径 B，未解析）——`ragdemo docs ingest-edgar --yes` 的实际工作。

从 CLI 里抽出来是为了能注入 HTTP 客户端做测试：CLI 自己构造指向 data.sec.gov /
www.sec.gov 的真实客户端，测试换成 MockTransport。

两类原始响应都留 provider_snapshot（CLAUDE.md §1.5）：
- 申报列表：JSON，内联存 jsonb；
- 申报全文：字节先放进 blob，快照只记 `response_ref` 指向它，文档行的
  `snapshot_id` 指向这一条——文档的原始来源就是这次全文拉取。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.edgar import EdgarAdapter, FilingRef
from ragdemo.adapters.http import HttpClient
from ragdemo.ingest.documents import DocumentWriter, DocumentWriteResult
from ragdemo.ingest.snapshot import record_blob_snapshot, record_snapshot
from ragdemo_core.blob import BlobStore

ARCHIVES_PROVIDER = "edgar-archives"


def list_filings(adapter: EdgarAdapter, *, cik: int, ingest_run_id: str) -> list[FilingRef]:
    """只列申报、不写库（dry run 用）。"""
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    return [ref for raw in adapter.fetch(ctx, cik=cik) for ref in adapter.parse_filings(raw)]


def ingest_edgar_filings(
    conn: psycopg.Connection,
    *,
    adapter: EdgarAdapter,
    archives_client: HttpClient,
    blob: BlobStore,
    cik: int,
    source: str,
    limit: int | None,
    ingest_run_id: str,
) -> list[tuple[FilingRef, DocumentWriteResult]]:
    """拉申报列表、逐份取全文存 blob、写 core.document；返回每份申报与它的写入结果。"""
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    refs: list[FilingRef] = []
    for raw in adapter.fetch(ctx, cik=cik):
        record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
        refs.extend(adapter.parse_filings(raw))
    if limit is not None:
        refs = refs[:limit]

    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source=source)
    results: list[tuple[FilingRef, DocumentWriteResult]] = []
    for ref in refs:
        fetched_at = datetime.now(UTC)
        body = adapter.fetch_full_text(ref, archives_client)
        blob_key = f"edgar/{ref.cik}/{ref.accession}/{ref.primary_document}"
        blob.put(blob_key, body)
        snapshot_id = record_blob_snapshot(
            conn,
            provider=ARCHIVES_PROVIDER,
            endpoint=ref.document_url,
            params={"cik": ref.cik, "accession": ref.accession, "form": ref.form_type},
            response_ref=blob_key,
            ingest_run_id=ingest_run_id,
            fetched_at=fetched_at,
            cost_cents=Decimal(0),  # SEC 公开数据免费；NULL 在成本账本里表示"单价未知"
        )
        doc = NormalizedDocument(
            provider_doc_id=ref.accession,
            entity_ref=str(cik),
            doc_type=ref.form_type,
            title=f"{ref.form_type} {ref.accession}",
            period=None,
            publish_at=ref.acceptance_datetime,
            language="en",
            source_url=ref.document_url,
            raw_bytes_ref=blob_key,
            content_hash=hashlib.sha256(body).hexdigest(),
            is_correction=False,
            supersedes_provider_doc_id=None,
            page_count=None,
            blocks=[],
            snapshot_id=snapshot_id,
        )
        results.append((ref, writer.write_document(doc, [], {})))
    return results
