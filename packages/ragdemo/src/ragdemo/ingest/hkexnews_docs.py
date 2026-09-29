"""港股公告全文入库（公告双源接入计划 Task 7）。

与 `ingest/cninfo_docs.py::run_cninfo_layer` 同一套编排（目录 + 选择性下载 + 按
`parse_policy.parse_tier` 判定的分批解析），复用同一个 `LayerReport` 形状与
`parse_tier` 判定函数——港股公告的中文标题（年报、中期报告、盈利警告等）适用
同一套规则；英文标题的公司暂时全部落 `ParseTier.NONE`（模块内 `parse_tier` 已经
对不含任何已知关键词的标题返回 `NONE`，不需要在这里另做英文判断）。

时点：`HkAnnouncementRef.publish_at` 已经是分钟精度的真实发布时刻（香港时间，
`adapters/hkexnews.py`），这里原样传给 `NormalizedDocument.publish_at` 与
`IndexEntry.known_at`——不像巨潮那样需要"当日 23:59"的保守上界换算。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.hkexnews import (
    HkAnnouncementRef,
    HkexnewsAdapter,
    HkexnewsListingIncomplete,
)
from ragdemo.adapters.http import HttpClient
from ragdemo.ingest.announcement_index import IndexEntry, IndexOutcome, IndexWriter
from ragdemo.ingest.assets_docs import (
    build_chunks_and_descriptions,
    prepare_documents,
    record_parse_snapshots,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.ingest.parse_policy import ESTIMATED_PAGES, LayerReport, ParseTier, parse_tier
from ragdemo.ingest.snapshot import record_blob_snapshot, record_snapshot
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber, TableDescriber
from ragdemo.parse.textin import DocumentParser, PageBudget
from ragdemo_core.blob import BlobStore

STATIC_PROVIDER = "hkexnews"


def collect_hk_refs(
    adapter: HkexnewsAdapter, raws: list[RawResponse], *, stock_code: str
) -> dict[str, HkAnnouncementRef]:
    """把各子窗口的响应合成按 NEWS_ID 去重的表，并与各完整子窗口自报的 `recordCnt`
    之和对账；对不上抛 `HkexnewsListingIncomplete`（目录少一条就是公告遗漏）。"""
    refs: dict[str, HkAnnouncementRef] = {}
    for raw in raws:
        for ref in adapter.parse_announcements(raw):
            refs[ref.news_id] = ref
    expected = adapter.expected_total(raws)
    if expected is not None and len(refs) != expected:
        raise HkexnewsListingIncomplete(
            f"{stock_code}：披露易自报 {expected} 条公告，实际列出 {len(refs)} 条"
        )
    return refs


@dataclass(frozen=True)
class HkexnewsLayerDeps:
    adapter: HkexnewsAdapter
    static_client: HttpClient  # 与查询同一个 host，仍然独立传入以保持同一个调用约定
    parser: DocumentParser
    blob: BlobStore
    describer: TableDescriber = field(default_factory=MockTableDescriber)


def run_hkexnews_layer(
    conn: psycopg.Connection,
    *,
    deps: HkexnewsLayerDeps,
    index: IndexWriter,
    entity_ref: str,
    stock_code: str,
    since: datetime,
    until: datetime,
    tiers: frozenset[ParseTier],
    budget: PageBudget,
    download: bool,
    ingest_run_id: str,
    dry_run: bool,
    cost_per_page_cny: Decimal | None = None,
) -> LayerReport:
    report = LayerReport()
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    raws: list[RawResponse] = []
    for raw in deps.adapter.fetch(ctx, stock_code=stock_code, since=since, until=until):
        if not dry_run:
            record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=deps.blob)
        raws.append(raw)
    refs = collect_hk_refs(deps.adapter, raws, stock_code=stock_code)
    report.listed = len(refs)
    report.source_total = deps.adapter.expected_total(raws)

    if dry_run:
        for ref in refs.values():
            if not ref.downloadable:
                continue
            tier = parse_tier(ref.title)
            report.estimated_pages[tier] = (
                report.estimated_pages.get(tier, 0) + ESTIMATED_PAGES[tier]
            )
        return report

    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source="hkexnews")
    chunk_cfg = ChunkConfig()
    describer = deps.describer
    pending: list[tuple[NormalizedDocument, int]] = []

    for ref in refs.values():
        can_download = download and ref.downloadable
        outcome, index_id = index.write(
            IndexEntry(
                entity_ref=entity_ref,
                source="hkexnews",
                source_ref=ref.news_id,
                title=ref.title,
                publish_date=ref.publish_at.date(),
                known_at=ref.publish_at,  # 分钟精度，已经是真实发布时刻
                has_full_text=can_download,
                url=f"https://www1.hkexnews.hk{ref.pdf_path}" if ref.downloadable else None,
            )
        )
        report.indexed += 1
        if not ref.downloadable:
            report.no_attachment += 1
        if not can_download:
            continue
        if outcome is IndexOutcome.SKIPPED and index.linked_doc_id(index_id) is not None:
            report.already_downloaded += 1
            continue

        body = deps.adapter.fetch_full_text(ref, deps.static_client)
        report.downloaded += 1
        content_hash = hashlib.sha256(body).hexdigest()
        blob_key = f"hkexnews/{ref.stock_code}/{content_hash}.pdf"
        deps.blob.put(blob_key, body)
        snapshot_id = record_blob_snapshot(
            conn,
            provider=STATIC_PROVIDER,
            endpoint=ref.pdf_path,
            params={"news_id": ref.news_id, "stock_code": ref.stock_code},
            response_ref=blob_key,
            ingest_run_id=ingest_run_id,
            fetched_at=datetime.now(UTC),
            cost_cents=Decimal(0),  # 官方披露平台公开数据免费
        )
        doc = NormalizedDocument(
            provider_doc_id=ref.news_id,
            entity_ref=entity_ref,
            doc_type="announcement",
            title=ref.title,
            period=None,
            publish_at=ref.publish_at,
            language="zh",
            source_url=f"https://www1.hkexnews.hk{ref.pdf_path}",
            raw_bytes_ref=blob_key,
            content_hash=content_hash,
            is_correction=False,
            supersedes_provider_doc_id=None,
            page_count=None,
            blocks=[],
            snapshot_id=snapshot_id,
        )

        if parse_tier(ref.title) not in tiers:
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
