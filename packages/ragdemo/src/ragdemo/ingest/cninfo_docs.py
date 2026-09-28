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
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from ragdemo.adapters.announcements import NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.cninfo import AnnouncementRef, CninfoAdapter
from ragdemo.adapters.http import HttpClient
from ragdemo.ingest.documents import DocumentWriter, DocumentWriteResult
from ragdemo.ingest.snapshot import record_blob_snapshot, record_snapshot
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
