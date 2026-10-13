"""存量表格网格回填（表格处理改进计划 T10）。

只读两样东西：库里已有的块，以及已缓存的 TextIn 响应 JSON（`parse_json_ref`）加
原始 PDF（`raw_ref`）。**不调 TextIn、不调 LLM、不调嵌入**，也不改任何已有行——
新增 `core.doc_table` 行；网格版本或校验版本变了才走 store 定义的 supersede 路径
（旧行标 `superseded_at`、插新行）。

整份文档一个事务：任何一张表对不上（数量 / 页码 / Markdown）整份不写。`write=False` 照常执行全部
逻辑（含真实 INSERT，以便让数据库约束也参与 dry run），最后回滚。

幂等：同一块已有 live 网格，且 `(grid_version, verify_version)` 与当前常量相同时跳过；唯有两种例外——
旧行是 `not_checked`（主 dagster 入库路径不给 blob）而这次能得到真实结论，以及 `reverify=True` 时
重算结果与库里不一致。`superseded_at` 取 `now()`，而公告的 `known_at` 是日精度（发布日结束），
当天内 `known_at > now()`，supersede 会违反 `doc_table_time_order`——这类行只记 `deferred`，不动。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from dataclasses import replace as dc_replace
from typing import Any, Final

import psycopg

from ragdemo.parse.textin import (
    _page_offset,
    page_dims_from_metrics,
    table_markdown,
)
from ragdemo.tables.context import (
    TableContext,
    bind_context,
    textin_caption,
    textin_preceding_texts,
)
from ragdemo.tables.grid import GRID_VERSION, GridError, TableGrid, grid_to_json
from ragdemo.tables.render import grid_html, grid_markdown
from ragdemo.tables.statements import statement_kind
from ragdemo.tables.store import (
    TableRecord,
    insert_table,
    live_table_meta,
    supersede_table,
)
from ragdemo.tables.textin_cells import grid_from_textin_item, textin_locator
from ragdemo.tables.verify_cells import (
    VERIFY_CELLS_VERSION,
    GridVerifySummary,
    PageWords,
    extract_page_words,
    summarize_grid,
    verify_grid,
    words_in_bbox,
)
from ragdemo_core.blob import BlobStore

MOCK_DESC: Final = re.compile(r"^.+表，列为 .+，共 \d+ 行。$")
LEGACY_DESC_MODEL: Final = "legacy:unrecorded"
MAX_SAMPLES_PER_DOC: Final = 20


class BackfillMismatch(RuntimeError):
    """JSON 表格条目与库里的表格块对不上；整份文档不写。"""


def desc_provenance_of(content_desc: str | None) -> tuple[str, str] | None:
    """库里没有记录描述的来源：Mock 模板句可以认出来，其余一律 `legacy:unrecorded`，不猜。"""
    if content_desc is None or not content_desc.strip():
        return None
    if MOCK_DESC.match(content_desc.strip()):
        return ("mock", "v1")
    return (LEGACY_DESC_MODEL, LEGACY_DESC_MODEL)


@dataclass(frozen=True)
class TableMatch:
    block_id: int
    item_index: int
    item: Mapping[str, Any]


def match_tables(
    detail: Sequence[Mapping[str, Any]], blocks: Sequence[tuple[int, int | None, str]]
) -> list[TableMatch]:
    """按 `blocks_from_detail` 的同一跳过规则枚举 JSON 表格条目，与块（按 ordinal）逐个配对。"""
    offset = _page_offset(detail)
    items: list[tuple[int, Mapping[str, Any], str]] = []
    for i, item in enumerate(detail):
        if int(item.get("content", 0) or 0) == 1:
            continue
        if str(item.get("type", "paragraph")) != "table":
            continue
        md = table_markdown(item.get("cells") or [])
        if not md.strip():
            continue
        items.append((i, item, md))
    if len(items) != len(blocks):
        raise BackfillMismatch(f"表格数量不等：JSON {len(items)} 张，库内现行块 {len(blocks)} 个")
    out: list[TableMatch] = []
    for (i, item, md), (block_id, page, content) in zip(items, blocks, strict=True):
        item_page = int(item["page_id"]) + offset if item.get("page_id") is not None else None
        if item_page != page:
            raise BackfillMismatch(
                f"块 {block_id} 页码不等：JSON 第 {item_page} 页，库内第 {page} 页"
            )
        if md != content:
            raise BackfillMismatch(f"块 {block_id} 的 Markdown 与 JSON 重建结果不相等")
        out.append(TableMatch(block_id, i, item))
    return out


@dataclass(frozen=True)
class DocBackfill:
    doc_id: int
    tables: int
    inserted: int
    skipped: int
    superseded: int
    md_equal: int
    html_equal: int
    split_pages_equal: int
    split_tables: int
    numeric_cells: int
    matched_cells: int
    mismatched_tables: int
    unit_bound: int
    numeric_tables: int
    page_in_grid: int = 0  # block.page ∈ grid.pages 的表数
    mismatched_cells: int = 0
    grid_failed: int = 0  # 网格构造失败（坏数据）的表：不写
    deferred: int = 0  # 需要 supersede 但 known_at >= now() 的表：不动
    pdf_ok: bool = True  # 原始 PDF 的文字层读到了
    samples: tuple[str, ...] = ()  # 不一致单元格样本（至多 20 条）


@dataclass
class BackfillReport:
    docs: list[DocBackfill]
    failed: dict[int, str]

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = {"docs": len(self.docs), "failed": len(self.failed)}
        for f in fields(DocBackfill):
            if f.name in ("doc_id", "pdf_ok", "samples"):
                continue
            out[f.name] = sum(int(getattr(d, f.name)) for d in self.docs)
        out["pdf_unavailable_docs"] = sum(1 for d in self.docs if not d.pdf_ok)
        return out


def _normalise(value: object) -> object:
    return json.loads(json.dumps(value, sort_keys=True))


def _units_dict(units: tuple[tuple[int, str], ...]) -> dict[str, str]:
    return {str(i): u for i, u in units}


def _fingerprint(rec: TableRecord) -> tuple[object, ...]:
    g, ctx, s = rec.grid, rec.context, rec.summary
    return (
        _normalise(grid_to_json(g)),
        ctx.caption,
        ctx.unit,
        ctx.unit_source,
        ctx.currency,
        _normalise(_units_dict(ctx.column_units)),
        _normalise(_units_dict(ctx.row_units)),
        rec.statement_kind,
        s.verdict,
        s.numeric,
        s.match,
        s.mismatch,
        s.unverifiable,
    )


def _stored_fingerprint(conn: psycopg.Connection[Any], table_id: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT grid, caption, unit, unit_source, currency, column_units, row_units,"
        " statement_kind, verify_verdict, cells_numeric, cells_match, cells_mismatch,"
        " cells_unverifiable"
        " FROM core.doc_table WHERE table_id = %s",
        (table_id,),
    ).fetchone()
    assert row is not None
    return (
        _normalise(row[0]),
        *row[1:5],
        _normalise(row[5]),
        _normalise(row[6]),
        *row[7:],
    )


def _mismatch_samples(
    block_id: int, verified: TableGrid, pages: Mapping[int, PageWords], room: int
) -> list[str]:
    out: list[str] = []
    for c in verified.cells:
        if len(out) >= room:
            break
        if c.check != "mismatch" or c.page is None or c.bbox is None:
            continue
        pw = pages.get(c.page)
        layer = " ".join(w.text for w in words_in_bbox(pw, c.bbox)) if pw is not None else ""
        out.append(
            f"block={block_id} page={c.page} r{c.row}c{c.col} cell={c.text!r} layer={layer!r}"
        )
    return out


def _backfill_in_txn(
    conn: psycopg.Connection[Any],
    blob: BlobStore,
    doc_id: int,
    *,
    ingest_run_id: str,
    reverify: bool,
) -> DocBackfill:
    doc = conn.execute(
        "SELECT parse_engine, parse_json_ref, raw_ref FROM core.document WHERE doc_id = %s",
        (doc_id,),
    ).fetchone()
    if doc is None:
        raise BackfillMismatch(f"文档 {doc_id} 不存在")
    engine, json_ref, raw_ref = doc
    if not json_ref or not engine:
        raise BackfillMismatch(f"文档 {doc_id} 没有 parse_json_ref / parse_engine")
    block_rows = conn.execute(
        "SELECT block_id, page, content, table_html, content_desc FROM core.doc_block"
        " WHERE doc_id = %s AND block_type = 'table' AND superseded_at IS NULL ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    payload = json.loads(blob.get(str(json_ref)))
    detail = (payload.get("result") or {}).get("detail") or []
    offset = _page_offset(detail)
    dims = page_dims_from_metrics(payload.get("metrics"), offset)
    matches = match_tables(detail, [(int(r[0]), r[1], str(r[2])) for r in block_rows])
    by_block = {int(r[0]): r for r in block_rows}

    grids: dict[int, TableGrid] = {}
    grid_failed = 0
    for m in matches:
        try:
            grid = grid_from_textin_item(
                m.item,
                page_offset=offset,
                page_dims=dims,
                engine=str(engine),
                provider_ref=textin_locator(str(json_ref), m.item),
            )
        except (GridError, ValueError, TypeError, KeyError):
            grid = None
        if grid is None:
            grid_failed += 1
        else:
            grids[m.block_id] = grid

    # 只读网格需要的那几页文字层，不解析整份 PDF。
    needed = {p for g in grids.values() for p in g.pages}
    pages: dict[int, PageWords] = {}
    pdf_ok = True
    if needed and raw_ref:
        try:
            pages = extract_page_words(blob.get(str(raw_ref)), needed)
        except Exception:  # 文字层读不出来：网格照写，记 not_checked，不阻断
            pdf_ok = False
    else:
        pdf_ok = bool(raw_ref) or not needed

    counts = {
        "inserted": 0,
        "skipped": 0,
        "superseded": 0,
        "deferred": 0,
        "md_equal": 0,
        "html_equal": 0,
        "split_tables": 0,
        "split_pages_equal": 0,
        "page_in_grid": 0,
        "numeric_cells": 0,
        "matched_cells": 0,
        "mismatched_cells": 0,
        "mismatched_tables": 0,
        "unit_bound": 0,
        "numeric_tables": 0,
    }
    samples: list[str] = []
    for m in matches:
        grid = grids.get(m.block_id)
        if grid is None:
            continue
        brow = by_block[m.block_id]
        block_page, content, table_html, content_desc = brow[1], str(brow[2]), brow[3], brow[4]
        counts["md_equal"] += int(grid_markdown(grid) == content)
        counts["html_equal"] += int(table_html is not None and grid_html(grid) == table_html)
        counts["page_in_grid"] += int(block_page in grid.pages)
        split = m.item.get("split_section_page_ids")
        if split:
            counts["split_tables"] += 1
            counts["split_pages_equal"] += int(set(grid.pages) == {int(p) + offset for p in split})

        verified = verify_grid(grid, pages) if pages else grid
        summary: GridVerifySummary = summarize_grid(verified, tier=None)
        context: TableContext = bind_context(
            verified,
            caption=textin_caption(detail, m.item),
            preceding_texts=textin_preceding_texts(detail, m.item_index),
        )
        prov = desc_provenance_of(content_desc)
        rec = TableRecord(
            block_id=m.block_id,
            grid=verified,
            context=context,
            summary=summary,
            statement_kind=statement_kind(verified),
            verify_version=VERIFY_CELLS_VERSION,
            desc_model=prov[0] if prov else None,
            desc_prompt_version=prov[1] if prov else None,
        )
        counts["numeric_cells"] += summary.numeric
        counts["matched_cells"] += summary.match
        counts["mismatched_cells"] += summary.mismatch
        counts["mismatched_tables"] += int(summary.mismatch > 0)
        counts["numeric_tables"] += int(summary.numeric > 0)
        counts["unit_bound"] += int(summary.numeric > 0 and context.unit_source != "none")
        if summary.mismatch and len(samples) < MAX_SAMPLES_PER_DOC:
            samples += _mismatch_samples(
                m.block_id, verified, pages, MAX_SAMPLES_PER_DOC - len(samples)
            )

        meta = live_table_meta(conn, m.block_id)
        replace = False
        if meta is None:
            pass
        else:
            table_id, grid_version, verify_version, verdict = meta
            stale = (grid_version, verify_version) != (GRID_VERSION, VERIFY_CELLS_VERSION)
            upgradable = verdict == "not_checked" and summary.verdict != "not_checked"
            replace = (
                stale
                or upgradable
                or (reverify and _stored_fingerprint(conn, table_id) != _fingerprint(rec))
            )
            if not replace:
                counts["skipped"] += 1
                continue
            row = conn.execute(
                "SELECT known_at < now() FROM core.doc_table WHERE table_id = %s", (table_id,)
            ).fetchone()
            if row is None or not row[0]:
                counts["deferred"] += 1  # superseded_at 必须晚于 known_at
                continue
            # 描述溯源（desc_*）与续表链接不是网格内容：重写时沿用旧行，不覆盖。
            prev = conn.execute(
                "SELECT desc_model, desc_prompt_version, continues_from_block_id"
                " FROM core.doc_table WHERE table_id = %s",
                (table_id,),
            ).fetchone()
            assert prev is not None
            rec = dc_replace(
                rec,
                desc_model=prev[0],
                desc_prompt_version=prev[1],
                continues_from_block_id=prev[2],
            )
            supersede_table(conn, table_id)
            counts["superseded"] += 1
        insert_table(conn, rec, ingest_run_id=ingest_run_id)
        counts["inserted"] += 1

    return DocBackfill(
        doc_id=doc_id,
        tables=len(matches),
        grid_failed=grid_failed,
        pdf_ok=pdf_ok,
        samples=tuple(samples),
        **counts,
    )


def backfill_document(
    conn: psycopg.Connection[Any],
    blob: BlobStore,
    doc_id: int,
    *,
    ingest_run_id: str,
    write: bool,
    reverify: bool = False,
) -> DocBackfill:
    """回填一份文档的全部现行表格块。`write=False`：算全部指标并回滚。"""
    result: DocBackfill | None = None
    with conn.transaction():
        result = _backfill_in_txn(
            conn, blob, doc_id, ingest_run_id=ingest_run_id, reverify=reverify
        )
        if not write:
            raise psycopg.Rollback()
    assert result is not None
    return result


def select_doc_ids(
    conn: psycopg.Connection[Any], doc_ids: Sequence[int] | None = None
) -> list[int]:
    """现行（未被取代）、TextIn 解析、有现行表格块的文档。"""
    rows = conn.execute(
        "SELECT DISTINCT d.doc_id FROM core.document d JOIN core.doc_block b USING (doc_id)"
        " WHERE d.superseded_at IS NULL AND d.parse_engine LIKE 'textin%%'"
        "   AND b.block_type = 'table' AND b.superseded_at IS NULL"
        "   AND (%(ids)s::bigint[] IS NULL OR d.doc_id = ANY(%(ids)s::bigint[]))"
        " ORDER BY 1",
        {"ids": list(doc_ids) if doc_ids is not None else None},
    ).fetchall()
    conn.commit()
    return [int(r[0]) for r in rows]


def run_backfill(
    conn: psycopg.Connection[Any],
    blob: BlobStore,
    *,
    ingest_run_id: str,
    write: bool,
    doc_ids: Sequence[int] | None = None,
    reverify: bool = False,
) -> BackfillReport:
    report = BackfillReport(docs=[], failed={})
    for doc_id in select_doc_ids(conn, doc_ids):
        try:
            report.docs.append(
                backfill_document(
                    conn,
                    blob,
                    doc_id,
                    ingest_run_id=ingest_run_id,
                    write=write,
                    reverify=reverify,
                )
            )
        except Exception as exc:  # 一份坏文档不拖累其余
            report.failed[doc_id] = f"{type(exc).__name__}: {exc}"
        conn.commit()
    return report


__all__ = [
    "LEGACY_DESC_MODEL",
    "MOCK_DESC",
    "VERIFY_CELLS_VERSION",
    "BackfillMismatch",
    "BackfillReport",
    "DocBackfill",
    "TableMatch",
    "backfill_document",
    "desc_provenance_of",
    "extract_page_words",
    "match_tables",
    "run_backfill",
    "select_doc_ids",
]
