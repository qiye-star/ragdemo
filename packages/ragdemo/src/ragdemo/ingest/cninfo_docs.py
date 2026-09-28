"""巨潮资讯网公告全文入库（路径 B，未解析）——`ragdemo docs ingest-cninfo --yes` 的实际工作。

与 `ingest/edgar_docs.py` 同一个理由从 CLI 里抽出来：能注入 HTTP 客户端做测试，
CLI 自己构造指向 `www.cninfo.com.cn` / `static.cninfo.com.cn` 的真实客户端，
测试换成 MockTransport。

两类原始响应都留 provider_snapshot（CLAUDE.md §1.5）：
- 公告列表：JSON，内联存 jsonb（`hisAnnouncement/query` 一页一条）；
- 公告 PDF：字节先放进 blob，快照只记 `response_ref` 指向它，文档行的
  `snapshot_id` 指向这一条——文档的原始来源就是这次 PDF 拉取，与 EDGAR
  全文入库的做法一致。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.cninfo import AnnouncementRef, CninfoAdapter
from ragdemo.adapters.http import HttpClient
from ragdemo.ingest.announcement_index import IndexEntry, IndexWriter, known_at_for_day
from ragdemo.ingest.assets_docs import (
    build_chunks_and_descriptions,
    prepare_documents,
    record_parse_snapshots,
)
from ragdemo.ingest.documents import DocumentWriter, DocumentWriteResult
from ragdemo.ingest.parse_policy import ESTIMATED_PAGES, LayerReport, ParseTier, parse_tier
from ragdemo.ingest.snapshot import record_blob_snapshot, record_snapshot
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.textin import DocumentParser, PageBudget
from ragdemo_core.blob import BlobStore

STATIC_PROVIDER = "cninfo-static"


def list_announcements(
    adapter: CninfoAdapter,
    *,
    stock_code: str,
    since: datetime,
    until: datetime,
    ingest_run_id: str,
) -> list[AnnouncementRef]:
    """只列公告、不写库（dry run 用）。跳过没有 PDF 附件的公告，去重
    （沪深两个 column 各查一遍可能对同一份公告各命中一次，实测未见过，
    但去重不依赖这个假设成立）。
    """
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    seen: dict[str, AnnouncementRef] = {}
    for raw in adapter.fetch(ctx, stock_code=stock_code, since=since, until=until):
        for ref in adapter.parse_announcements(raw):
            seen[ref.announcement_id] = ref
    return list(seen.values())


def ingest_cninfo_announcements(
    conn: psycopg.Connection,
    *,
    adapter: CninfoAdapter,
    static_client: HttpClient,
    blob: BlobStore,
    stock_code: str,
    entity_ref: str,
    source: str,
    since: datetime,
    until: datetime,
    limit: int | None,
    ingest_run_id: str,
) -> list[tuple[AnnouncementRef, DocumentWriteResult]]:
    """拉公告列表、逐份取 PDF 存 blob、写 core.document；返回每份公告与它的写入结果。"""
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    refs: dict[str, AnnouncementRef] = {}
    for raw in adapter.fetch(ctx, stock_code=stock_code, since=since, until=until):
        record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
        for ref in adapter.parse_announcements(raw):
            refs[ref.announcement_id] = ref
    ordered = list(refs.values())
    if limit is not None:
        ordered = ordered[:limit]

    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source=source)
    results: list[tuple[AnnouncementRef, DocumentWriteResult]] = []
    for ref in ordered:
        fetched_at = datetime.now(UTC)
        body = adapter.fetch_full_text(ref, static_client)
        content_hash = hashlib.sha256(body).hexdigest()
        blob_key = f"cninfo/{ref.sec_code}/{content_hash}.pdf"
        blob.put(blob_key, body)
        snapshot_id = record_blob_snapshot(
            conn,
            provider=STATIC_PROVIDER,
            endpoint=ref.pdf_path,
            params={"announcement_id": ref.announcement_id, "sec_code": ref.sec_code},
            response_ref=blob_key,
            ingest_run_id=ingest_run_id,
            fetched_at=fetched_at,
            cost_cents=Decimal(0),  # 官方披露平台公开数据免费
        )
        doc = NormalizedDocument(
            provider_doc_id=ref.announcement_id,
            entity_ref=entity_ref,
            doc_type="announcement",
            title=ref.title,
            period=None,
            # 巨潮的 announcementTime 只精确到日，这里原样传，DocumentWriter
            # 按 core.source_registry.cninfo.time_precision='day' 换算成
            # 当日 23:59:59.999999（北京时间）的保守上界（adapters/cninfo.py
            # 模块 docstring）。
            publish_at=ref.publish_at,
            language="zh",
            source_url=f"https://static.cninfo.com.cn{ref.pdf_path}",
            raw_bytes_ref=blob_key,
            content_hash=content_hash,
            is_correction=False,
            supersedes_provider_doc_id=None,
            page_count=None,
            blocks=[],
            snapshot_id=snapshot_id,
        )
        results.append((ref, writer.write_document(doc, [], {})))
    return results


# --- run_cninfo_layer：目录 + 选择性下载 + 按策略解析（公告双源接入计划 Task 4）------


@dataclass(frozen=True)
class CninfoLayerDeps:
    adapter: CninfoAdapter
    static_client: HttpClient  # 指向 static.cninfo.com.cn
    parser: DocumentParser
    blob: BlobStore


def run_cninfo_layer(
    conn: psycopg.Connection,
    *,
    deps: CninfoLayerDeps,
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
) -> LayerReport:
    """巨潮层：列目录，按 `download` 决定要不要下载 PDF，按 `tiers` 决定下载下来的
    PDF 要不要送去解析。

    `download=False`：只写目录行（`has_full_text=False`，`url` 指向巨潮的公告详情
    页），不下载、不写 `core.document`——用于本次不在试点范围内的实体（D2）。

    `download=True`：每份公告都下载 PDF、写一行 `core.document`；只有
    `parse_tier(title) in tiers` 的那些会被送去解析（有块），其余落一行没有块的
    `document`（与旧版 `ingest_cninfo_announcements` 一致，只是现在多了目录行）。
    预算耗尽时 `prepare_documents` 会提前停止，没轮到的那些也落无块行，计入
    `report.skipped_budget`，不会凭空消失——它们仍然在 `core.document` 里占着一行，
    下次 `docs parse-pending` 能捡起来。

    `dry_run=True`：只列目录，不下载、不写库，页数按档位经验值估算。
    """
    report = LayerReport()
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=date.today())
    refs: dict[str, AnnouncementRef] = {}
    for raw in deps.adapter.fetch(ctx, stock_code=stock_code, since=since, until=until):
        if not dry_run:
            record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=deps.blob)
        for ref in deps.adapter.parse_announcements(raw):
            refs[ref.announcement_id] = ref
    report.listed = len(refs)

    if dry_run:
        for ref in refs.values():
            tier = parse_tier(ref.title)
            report.estimated_pages[tier] = (
                report.estimated_pages.get(tier, 0) + ESTIMATED_PAGES[tier]
            )
        return report

    writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source="cninfo")
    chunk_cfg = ChunkConfig()
    describer = MockTableDescriber()
    pending: list[tuple[NormalizedDocument, int]] = []  # (待解析文档, 对应目录行 id)

    for ref in refs.values():
        known_at = known_at_for_day(ref.publish_at.date())
        _outcome, index_id = index.write(
            IndexEntry(
                entity_ref=entity_ref,
                source="cninfo",
                source_ref=ref.announcement_id,
                title=ref.title,
                publish_date=ref.publish_at.date(),
                known_at=known_at,
                has_full_text=download,
                url=f"https://static.cninfo.com.cn{ref.pdf_path}",
            )
        )
        report.indexed += 1
        if not download:
            continue

        body = deps.adapter.fetch_full_text(ref, deps.static_client)
        report.downloaded += 1
        content_hash = hashlib.sha256(body).hexdigest()
        blob_key = f"cninfo/{ref.sec_code}/{content_hash}.pdf"
        deps.blob.put(blob_key, body)
        snapshot_id = record_blob_snapshot(
            conn,
            provider=STATIC_PROVIDER,
            endpoint=ref.pdf_path,
            params={"announcement_id": ref.announcement_id, "sec_code": ref.sec_code},
            response_ref=blob_key,
            ingest_run_id=ingest_run_id,
            fetched_at=datetime.now(UTC),
            cost_cents=Decimal(0),  # 官方披露平台公开数据免费
        )
        doc = NormalizedDocument(
            provider_doc_id=ref.announcement_id,
            entity_ref=entity_ref,
            doc_type="announcement",
            title=ref.title,
            period=None,
            publish_at=known_at,
            language="zh",
            source_url=f"https://static.cninfo.com.cn{ref.pdf_path}",
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
        record_parse_snapshots(conn, prepared, ingest_run_id=ingest_run_id, cost_per_page_cny=None)
        for item, (_, index_id) in zip(prepared, pending, strict=False):
            chunks, descriptions, _violations = build_chunks_and_descriptions(
                item.doc, chunk_cfg, describer
            )
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
        # prepare_documents 在预算耗尽时提前停止，没轮到的那些落一行无块 document，
        # 不能悄悄丢掉——它们仍然是"需要解析"的公告，只是这次没有预算。
        for doc, index_id in pending[len(prepared) :]:
            result = writer.write_document(doc, [], {})
            index.link_document(index_id, result.doc_id)
            report.skipped_budget += 1

    return report
