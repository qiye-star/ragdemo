"""EDGAR 申报接入公告目录（公告双源接入计划 Task 8）。

复用既有 `adapters/edgar.py`（列表、`known_at`、全文抓取）与
`ingest/edgar_docs.py::ingest_edgar_filings` 已经验证过的写库路径，这里只补两件
它们此前没做的事：CIK 解析（entity_ref 在种子里可能还没有 `edgar_cik`）、以及把
每份申报也写进 `core.announcement_index`（此前 `ingest_edgar_filings` 只写
`core.document`，从不出现在任何目录表里）。

时点：`FilingRef.acceptance_datetime` 已经是秒级的真实受理时刻
（`EdgarAdapter.known_at`），这里原样传给 `IndexEntry.known_at`，不做任何
"当日 23:59" 之类的保守上界换算——那只对日精度的源成立。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.edgar import EdgarAdapter, FilingRef
from ragdemo.adapters.http import HttpClient
from ragdemo.ingest.announcement_index import IndexEntry, IndexWriter
from ragdemo.ingest.assets_docs import (
    build_chunks_and_descriptions,
    prepare_documents,
    record_parse_snapshots,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.ingest.parse_policy import ESTIMATED_PAGES, LayerReport, ParseTier, parse_tier_for_form
from ragdemo.ingest.snapshot import record_blob_snapshot, record_snapshot
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.textin import DocumentParser, PageBudget
from ragdemo_core.blob import BlobStore

ARCHIVES_PROVIDER = "edgar-archives"


def resolve_cik(client: HttpClient, *, ticker: str) -> int | None:
    """查 SEC `files/company_tickers.json`（免鉴权静态文件，2026-09-28 实测：无联系
    方式的 User-Agent 返回 403，带联系方式的返回 200——这不是我们编的限制，是 SEC
    会执行的门槛，`client` 必须已经带上诚实 UA）。按 ticker 精确匹配；找不到返回
    `None`，调用方决定是失败还是跳过。"""
    raw = client.get_json("/files/company_tickers.json", {})
    rows: dict[str, Any] = raw.payload if isinstance(raw.payload, dict) else {}
    for row in rows.values():
        if str(row.get("ticker")) == ticker:
            return int(row["cik_str"])
    return None


@dataclass(frozen=True)
class EdgarLayerDeps:
    adapter: EdgarAdapter
    archives_client: HttpClient  # 指向 www.sec.gov（EdgarAdapter.fetch_full_text 用它，
    # 与 adapter 自己指向 data.sec.gov 的客户端是两个 host）
    parser: DocumentParser
    blob: BlobStore


def run_edgar_layer(
    conn: psycopg.Connection,
    *,
    deps: EdgarLayerDeps,
    index: IndexWriter,
    entity_ref: str,
    cik: int,
    tiers: frozenset[ParseTier],
    budget: PageBudget,
    download: bool,
    ingest_run_id: str,
    dry_run: bool,
    since: date | None = None,
    cost_per_page_cny: Decimal | None = None,
) -> LayerReport:
    report = LayerReport()
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    refs: dict[str, FilingRef] = {}
    for raw in deps.adapter.fetch(ctx, cik=cik, since=since):
        if not dry_run:
            record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=deps.blob)
        for ref in deps.adapter.parse_filings(raw):
            refs[ref.accession] = ref
    report.listed = len(refs)

    if dry_run:
        for ref in refs.values():
            tier = parse_tier_for_form(ref.form_type)
            report.estimated_pages[tier] = (
                report.estimated_pages.get(tier, 0) + ESTIMATED_PAGES[tier]
            )
        return report

    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source="edgar")
    chunk_cfg = ChunkConfig()
    describer = MockTableDescriber()
    pending: list[tuple[NormalizedDocument, int]] = []

    for ref in refs.values():
        title = f"{ref.form_type} {ref.accession}"
        _outcome, index_id = index.write(
            IndexEntry(
                entity_ref=entity_ref,
                source="edgar",
                source_ref=ref.accession,
                title=title,
                publish_date=ref.acceptance_datetime.date(),
                known_at=ref.acceptance_datetime,  # 秒级真实受理时刻，原样存
                has_full_text=download,
                url=ref.document_url,
            )
        )
        report.indexed += 1
        if not download:
            continue

        body = deps.adapter.fetch_full_text(ref, deps.archives_client)
        report.downloaded += 1
        blob_key = f"edgar/{ref.cik}/{ref.accession}/{ref.primary_document}"
        deps.blob.put(blob_key, body)
        snapshot_id = record_blob_snapshot(
            conn,
            provider=ARCHIVES_PROVIDER,
            endpoint=ref.document_url,
            params={"cik": ref.cik, "accession": ref.accession, "form": ref.form_type},
            response_ref=blob_key,
            ingest_run_id=ingest_run_id,
            fetched_at=datetime.now(UTC),
            cost_cents=Decimal(0),  # SEC 公开数据免费
        )
        doc = NormalizedDocument(
            provider_doc_id=ref.accession,
            entity_ref=entity_ref,
            doc_type=ref.form_type,
            title=title,
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

        if parse_tier_for_form(ref.form_type) not in tiers:
            result = writer.write_document(doc, [], {})
            index.link_document(index_id, result.doc_id)
            report.stored_unparsed += 1
            continue
        pending.append((doc, index_id))

    if pending:
        docs_only = [doc for doc, _ in pending]
        prepared = prepare_documents(docs_only, deps.parser, deps.blob, budget, owner_user=None)
        record_parse_snapshots(
            conn, prepared, ingest_run_id=ingest_run_id, cost_per_page_cny=cost_per_page_cny
        )
        for item, (_, index_id) in zip(prepared, pending, strict=False):
            chunks, descriptions, violations = build_chunks_and_descriptions(
                item.doc, chunk_cfg, describer
            )
            if violations:
                result = writer.write_document(replace(item.doc, blocks=[]), [], {})
                index.link_document(index_id, result.doc_id)
                report.validation_failed += 1
                continue
            result = writer.write_document(
                item.doc,
                chunks,
                descriptions,
                artifacts=item.artifacts,
                chunking_version=chunk_cfg.version,
            )
            index.link_document(index_id, result.doc_id)
            if item.doc.blocks:
                report.parsed_docs += 1
                report.parsed_pages += item.billed_pages
            else:
                report.stored_unparsed += 1
        for doc, index_id in pending[len(prepared) :]:
            result = writer.write_document(doc, [], {})
            index.link_document(index_id, result.doc_id)
            report.skipped_budget += 1

    return report
