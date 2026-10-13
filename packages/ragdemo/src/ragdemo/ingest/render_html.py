"""EDGAR htm 离线转 PDF 并留来源（EDGAR 8-K 转 PDF 方案 §4.2 / §4.3 A）。

- 原始 htm 永不改动；PDF 是派生物，放在同目录、换成 `.rendered.pdf` 后缀。
- 一旦写入就不覆盖（渲染不可逐字节复现，以存下来的那份为准）。
- 来源记在 `core.provider_snapshot`（零迁移）：`provider='local-html-render'`，
  params 含 `source_raw_ref` / `source_sha256`（htm 的 content_hash）/ `pdf_sha256` /
  `renderer` / `page_count`，`response_ref` 指向派生 PDF，`cost_cents=0`（免费）。
- 免费、离线：不调用 TextIn，不做任何外部请求。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import psycopg

from ragdemo.adapters.html_render import HtmlRenderer
from ragdemo.ingest.pdf_pages import pdf_page_count
from ragdemo.ingest.snapshot import record_blob_snapshot
from ragdemo_core.blob import BlobStore

RENDER_PROVIDER = "local-html-render"
RENDER_ENDPOINT = "msedge --print-to-pdf"
HTML_SUFFIXES = (".htm", ".html")
DERIVED_SUFFIX = ".rendered.pdf"
UNKNOWN_RENDERER = "unknown:pre-existing-pdf"


class RenderHtmlError(RuntimeError):
    pass


def is_html_ref(raw_ref: str) -> bool:
    return raw_ref.lower().endswith(HTML_SUFFIXES)


def derived_pdf_key(raw_ref: str) -> str:
    """`.../x.htm` -> `.../x.rendered.pdf`。"""
    if not is_html_ref(raw_ref):
        raise RenderHtmlError(f"raw_ref 不是 htm/html：{raw_ref!r}")
    stem = raw_ref[: raw_ref.rfind(".")]
    return stem + DERIVED_SUFFIX


def find_render_snapshot(conn: psycopg.Connection, pdf_key: str) -> int | None:
    row = conn.execute(
        "SELECT snapshot_id FROM core.provider_snapshot"
        " WHERE provider = %s AND response_ref = %s ORDER BY snapshot_id DESC LIMIT 1",
        (RENDER_PROVIDER, pdf_key),
    ).fetchone()
    return None if row is None else int(row[0])


@dataclass(frozen=True)
class RenderOutcome:
    doc_id: int
    status: Literal["rendered", "skipped", "dry_run"]
    pdf_key: str
    page_count: int | None = None
    snapshot_id: int | None = None


def _record(
    conn: psycopg.Connection,
    *,
    raw_ref: str,
    source_sha256: str,
    pdf: bytes,
    pdf_key: str,
    renderer: str,
    page_count: int | None,
    ingest_run_id: str,
) -> int:
    return record_blob_snapshot(
        conn,
        provider=RENDER_PROVIDER,
        endpoint=RENDER_ENDPOINT,
        params={
            "source_raw_ref": raw_ref,
            "source_sha256": source_sha256,
            "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
            "renderer": renderer,
            "page_count": page_count,
        },
        response_ref=pdf_key,
        ingest_run_id=ingest_run_id,
        cost_cents=Decimal(0),
    )


def render_html_document(
    conn: psycopg.Connection,
    *,
    doc_id: int,
    renderer: HtmlRenderer,
    blob: BlobStore,
    ingest_run_id: str,
    dry_run: bool,
) -> RenderOutcome:
    row = conn.execute(
        "SELECT raw_ref, content_hash, superseded_at FROM core.document WHERE doc_id = %s",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise RenderHtmlError(f"doc_id={doc_id} 不存在")
    raw_ref, content_hash, superseded_at = row
    if superseded_at is not None:
        raise RenderHtmlError(f"doc_id={doc_id} 已被取代，不是现行版本")
    if raw_ref is None or not is_html_ref(raw_ref):
        raise RenderHtmlError(f"doc_id={doc_id} 的 raw_ref 不是 htm/html：{raw_ref!r}")
    key = derived_pdf_key(raw_ref)

    if blob.exists(key):
        existing = blob.get(key)
        pages = pdf_page_count(existing)
        snap = find_render_snapshot(conn, key)
        if snap is None and not dry_run:
            # PDF 已在、快照缺（上次写完 blob 没来得及写快照）：不重渲染，只补快照。
            snap = _record(
                conn,
                raw_ref=raw_ref,
                source_sha256=content_hash,
                pdf=existing,
                pdf_key=key,
                renderer=UNKNOWN_RENDERER,
                page_count=pages,
                ingest_run_id=ingest_run_id,
            )
        return RenderOutcome(doc_id, "skipped", key, pages, snap)

    if dry_run:
        return RenderOutcome(doc_id, "dry_run", key)

    html = blob.get(raw_ref)
    rendered = renderer.render(html)
    pages = pdf_page_count(rendered.pdf)
    if pages is None:
        raise RenderHtmlError("渲染产物页数读不出来，不入库")
    blob.put(key, rendered.pdf)
    snap = _record(
        conn,
        raw_ref=raw_ref,
        source_sha256=content_hash,
        pdf=rendered.pdf,
        pdf_key=key,
        renderer=rendered.renderer,
        page_count=pages,
        ingest_run_id=ingest_run_id,
    )
    return RenderOutcome(doc_id, "rendered", key, pages, snap)
