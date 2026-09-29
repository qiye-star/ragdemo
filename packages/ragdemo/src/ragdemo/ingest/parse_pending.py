"""解析已存原文、尚未切块的文档（公告双源接入计划 Task 10）。

`docs announcements`（Task 9）按 `DOWNLOAD_ENTITIES` 只对试点实体下载 PDF；就算下载了，
`tiers` 也可能只请求 `ParseTier.FIRST`，落下一批"有 raw_ref、没有块"的文档——这是刻意的
（先省钱，效果好再扩大范围）。本模块是"以后扩大范围"的落地入口：不重新抓取任何东西，
只对已经躺在 blob 里的原文重新走一遍 `prepare_documents → reparse_document`。

`known_at` 沿用被取代的原文档——原文在公告日就已公开，晚解析不改变可知时刻
（`DocumentWriter.reparse_document` 已经保证这一点，这里不用重复处理）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.config import load_config
from ragdemo.ingest.assets_docs import (
    build_chunks_and_descriptions,
    prepare_documents,
    record_parse_snapshots,
)
from ragdemo.ingest.documents import DocumentWriter
from ragdemo.ingest.parse_policy import ESTIMATED_PAGES, ParseTier, parse_tier, parse_tier_for_form
from ragdemo.ingest.pdf_pages import pdf_page_count
from ragdemo.ingest.writer import UnknownEntityRef
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describer_factory import build_describer
from ragdemo.parse.textin import DocumentParser, PageBudget
from ragdemo_core.blob import BlobNotFound, BlobStore

_PENDING_SOURCES = ("cninfo", "hkexnews", "edgar")


@dataclass
class ParsePendingReport:
    selected: int = 0
    reparsed: int = 0
    parsed_pages: int = 0
    stored_unparsed: int = 0
    skipped_budget: int = 0
    # 重解析结果没通过校验（最常见：content_desc 生成被拒绝）——保留原文档不动。
    validation_failed: int = 0
    # dry run 专用：真实页数（读得到 PDF 时）或经验估算（读不到时）按档位汇总。
    estimated_pages: dict[ParseTier, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingDocument:
    doc_id: int
    entity_id: str
    source: str
    source_ref: str | None
    doc_type: str
    title: str
    period: str | None
    publish_at: object  # datetime；避免在此模块重新 import 只为标注类型
    language: str
    source_url: str | None
    raw_ref: str
    content_hash: str
    snapshot_id: int | None


def tier_for_document(source: str, doc_type: str, title: str) -> ParseTier:
    """EDGAR 的标题是英文表单名，中文标题规则（`parse_tier`）在它身上没有意义——
    按 `doc_type`（即 form_type）直接查档位表；cninfo/hkexnews 沿用既有的标题规则。"""
    if source == "edgar":
        return parse_tier_for_form(doc_type)
    return parse_tier(title)


def _entity_ref_for(conn: psycopg.Connection, entity_id: str) -> str:
    """反查 entity_id 对应的任意一个供应商代码列，供重解析构造
    `NormalizedDocument.entity_ref`——`DocumentWriter._resolve_entity_id` 只认
    供应商代码做匹配，不能直接把 entity_id 传回去。"""
    row = conn.execute(
        "SELECT tushare_code, ifind_code, wind_code, edgar_cik"
        "  FROM core.entity WHERE entity_id = %s",
        (entity_id,),
    ).fetchone()
    if row is None or all(v is None for v in row):
        raise UnknownEntityRef(f"entity_id {entity_id!r} 在 core.entity 里没有任何供应商代码列")
    return str(next(v for v in row if v is not None))


def _resolve_entity_id(conn: psycopg.Connection, entity_ref: str) -> str:
    row = conn.execute(
        "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
        " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
        (entity_ref, entity_ref, entity_ref, entity_ref),
    ).fetchone()
    if row is None:
        raise UnknownEntityRef(f"无法解析实体代码 {entity_ref!r} 为 entity_id")
    return str(row[0])


def _select_pending(conn: psycopg.Connection, entity_id: str | None) -> list[_PendingDocument]:
    rows = conn.execute(
        "SELECT d.doc_id, d.entity_id, d.source, d.source_ref, d.doc_type, d.title,"
        "       d.period, d.publish_at, d.language, d.source_url, d.raw_ref,"
        "       d.content_hash, d.snapshot_id"
        "  FROM core.document d"
        " WHERE d.superseded_at IS NULL"
        "   AND d.raw_ref IS NOT NULL"
        "   AND d.source = ANY(%s)"
        "   AND (%s::text IS NULL OR d.entity_id = %s)"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM core.doc_block b"
        "      WHERE b.doc_id = d.doc_id AND b.superseded_at IS NULL"
        "   )"
        " ORDER BY d.publish_at",
        (list(_PENDING_SOURCES), entity_id, entity_id),
    ).fetchall()
    return [
        _PendingDocument(
            doc_id=r[0],
            entity_id=r[1],
            source=r[2],
            source_ref=r[3],
            doc_type=r[4],
            title=r[5],
            period=r[6],
            publish_at=r[7],
            language=r[8],
            source_url=r[9],
            raw_ref=r[10],
            content_hash=r[11],
            snapshot_id=r[12],
        )
        for r in rows
    ]


def run_parse_pending(
    conn: psycopg.Connection,
    *,
    parser: DocumentParser,
    blob: BlobStore,
    entity_ref: str | None,
    tiers: frozenset[ParseTier],
    budget: PageBudget,
    ingest_run_id: str,
    dry_run: bool,
) -> ParsePendingReport:
    entity_id = _resolve_entity_id(conn, entity_ref) if entity_ref is not None else None
    candidates = [
        row
        for row in _select_pending(conn, entity_id)
        if tier_for_document(row.source, row.doc_type, row.title) in tiers
    ]

    report = ParsePendingReport(selected=len(candidates))

    if dry_run:
        for row in candidates:
            tier = tier_for_document(row.source, row.doc_type, row.title)
            pages = None
            try:
                pages = pdf_page_count(blob.get(row.raw_ref))
            except BlobNotFound:
                pages = None
            report.estimated_pages[tier] = report.estimated_pages.get(tier, 0) + (
                pages or ESTIMATED_PAGES[tier]
            )
        return report

    chunk_cfg = ChunkConfig()
    describer = build_describer(load_config())

    for row in candidates:
        if budget.exhausted:
            report.skipped_budget += 1
            continue

        doc = NormalizedDocument(
            provider_doc_id=row.source_ref or str(row.doc_id),
            entity_ref=_entity_ref_for(conn, row.entity_id),
            doc_type=row.doc_type,
            title=row.title,
            period=row.period,
            publish_at=row.publish_at,  # type: ignore[arg-type]
            language=row.language,
            source_url=row.source_url,
            raw_bytes_ref=row.raw_ref,
            content_hash=row.content_hash,
            is_correction=False,
            supersedes_provider_doc_id=None,
            page_count=None,
            blocks=[],
            snapshot_id=row.snapshot_id,
        )

        prepared = prepare_documents([doc], parser, blob, budget, owner_user=None)
        record_parse_snapshots(conn, prepared, ingest_run_id=ingest_run_id, cost_per_page_cny=None)
        if not prepared:
            # 预算在这份文档身上耗尽（prepare_documents 在调用 parser 前检查），
            # 或者是 ParseRetryable——两种都不留记号，下次跑自然重试。
            report.skipped_budget += 1
            continue

        item = prepared[0]
        chunks, descriptions, violations = build_chunks_and_descriptions(
            item.doc, chunk_cfg, describer
        )
        if violations:
            # 重解析结果没通过校验（最常见：某个表格的 content_desc 生成被
            # describe.py 的违禁词过滤拒绝）——保留原来那份无块文档不动，不拿一个
            # 更差的结果去取代它；下次跑再试一次，与 tier_c_reparse 遇到同样情况
            # 时的处理一致（见该资产对 validate_chunks 失败的注释）。
            report.validation_failed += 1
            continue
        writer = DocumentWriter(conn, ingest_run_id=ingest_run_id, source=row.source)
        result = writer.reparse_document(
            item.doc,
            chunks,
            descriptions,
            supersedes_doc_id=row.doc_id,
            artifacts=item.artifacts,
            chunking_version=chunk_cfg.version,
        )

        index_row = conn.execute(
            "SELECT index_id FROM core.announcement_index"
            " WHERE doc_id = %s AND superseded_at IS NULL",
            (row.doc_id,),
        ).fetchone()
        if index_row is not None:
            conn.execute(
                "UPDATE core.announcement_index SET doc_id = %s WHERE index_id = %s",
                (result.doc_id, index_row[0]),
            )
            conn.commit()

        if item.doc.blocks:
            report.reparsed += 1
            report.parsed_pages += item.billed_pages
        else:
            report.stored_unparsed += 1

    return report
