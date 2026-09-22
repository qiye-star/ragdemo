"""文档查询：SQL + 行映射。不 import fastapi。"""

from __future__ import annotations

from datetime import datetime

import psycopg

from ragdemo.api.schemas import (
    BlockTypeCount,
    DocumentDetail,
    DocumentSummary,
    LayoutBlock,
    LayoutResponse,
    PageBlockCount,
)
from ragdemo.api.serialize import (
    as_bbox,
    as_bool,
    as_datetime,
    as_int,
    as_optional_float,
    as_optional_str,
    as_str,
    as_str_list,
)

_PREVIEW_CHARS = 120

# 显式列清单，不用 SELECT *：这条查询给列表页用，只暴露文档级元数据，
# 不带 content——那属于单块详情接口的范围。
_DOCUMENT_COLUMNS = """
    d.doc_id, d.entity_id, d.doc_type, d.title, d.period, d.publish_at, d.language,
    d.source, d.page_count, d.parse_engine, d.parse_confidence, d.known_at,
    d.version_group_id, d.is_correction, d.supersedes_doc_id, d.parse_warnings,
    d.can_show_raw, d.time_precision
"""

_LIST_DOCUMENTS_SQL = f"""
    SELECT {_DOCUMENT_COLUMNS},
           (SELECT count(*) FROM asof.doc_block b WHERE b.doc_id = d.doc_id) AS block_count,
           (SELECT count(*) FROM asof.doc_block b
             WHERE b.doc_id = d.doc_id AND b.is_leaf) AS leaf_count
      FROM asof.document d
     WHERE d.owner_tenant IS NULL AND d.owner_user IS NULL
       AND (%(entity_id)s::text IS NULL OR d.entity_id = %(entity_id)s)
       AND (%(doc_type)s::text  IS NULL OR d.doc_type  = %(doc_type)s)
       AND (%(source)s::text    IS NULL OR d.source    = %(source)s)
     ORDER BY d.publish_at DESC, d.doc_id
     LIMIT %(limit)s OFFSET %(offset)s
"""

_GET_DOCUMENT_SQL = f"""
    SELECT {_DOCUMENT_COLUMNS},
           (SELECT count(*) FROM asof.doc_block b WHERE b.doc_id = d.doc_id) AS block_count,
           (SELECT count(*) FROM asof.doc_block b
             WHERE b.doc_id = d.doc_id AND b.is_leaf) AS leaf_count
      FROM asof.document d
     WHERE d.doc_id = %(doc_id)s
       AND d.owner_tenant IS NULL AND d.owner_user IS NULL
"""

_BLOCK_TYPE_COUNTS_SQL = """
    SELECT block_type::text, count(*),
           count(*) FILTER (WHERE bbox IS NOT NULL),
           count(*) FILTER (WHERE is_leaf)
      FROM asof.doc_block
     WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL
     GROUP BY 1 ORDER BY 1
"""

_PAGE_BLOCK_COUNTS_SQL = """
    SELECT page, count(*)
      FROM asof.doc_block
     WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL
     GROUP BY page ORDER BY page NULLS LAST
"""

_PAGE_LAYOUT_SQL = """
    SELECT block_id, parent_block_id, block_type::text, section_path, ordinal,
           page, bbox, is_leaf, char_len, parse_confidence,
           left(content, %(preview_chars)s) AS preview,
           (content_desc IS NOT NULL) AS has_desc,
           (table_html IS NOT NULL) AS has_table_html
      FROM asof.doc_block
     WHERE doc_id = %(doc_id)s AND page = %(page)s
       AND owner_tenant IS NULL AND owner_user IS NULL
     ORDER BY ordinal
"""


def _row_to_summary(row: tuple[object, ...]) -> DocumentSummary:
    return DocumentSummary(
        doc_id=as_int(row[0]),
        entity_id=as_optional_str(row[1]),
        doc_type=as_str(row[2]),
        title=as_str(row[3]),
        period=as_optional_str(row[4]),
        publish_at=as_datetime(row[5]).isoformat(),
        language=as_str(row[6]),
        source=as_str(row[7]),
        page_count=as_int(row[8]) if row[8] is not None else None,
        parse_engine=as_optional_str(row[9]),
        parse_confidence=as_optional_float(row[10]),
        known_at=as_datetime(row[11]).isoformat(),
        version_group_id=as_int(row[12]),
        is_correction=as_bool(row[13]),
        supersedes_doc_id=as_int(row[14]) if row[14] is not None else None,
        parse_warnings=as_str_list(row[15]),
        can_show_raw=as_bool(row[16]),
        time_precision=as_optional_str(row[17]),
        block_count=as_int(row[18]),
        leaf_count=as_int(row[19]),
    )


def list_documents(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    entity_id: str | None = None,
    doc_type: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DocumentSummary]:
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        rows = conn.execute(
            _LIST_DOCUMENTS_SQL,
            {
                "entity_id": entity_id,
                "doc_type": doc_type,
                "source": source,
                "limit": limit,
                "offset": offset,
            },
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def get_document(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    doc_id: int,
    as_of: datetime,
) -> DocumentDetail | None:
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        row = conn.execute(_GET_DOCUMENT_SQL, {"doc_id": doc_id}).fetchone()
        if row is None:
            return None
        type_rows = conn.execute(_BLOCK_TYPE_COUNTS_SQL, {"doc_id": doc_id}).fetchall()
        page_rows = conn.execute(_PAGE_BLOCK_COUNTS_SQL, {"doc_id": doc_id}).fetchall()

    summary = _row_to_summary(row)
    return DocumentDetail(
        **summary.model_dump(),
        block_type_counts=[
            BlockTypeCount(
                block_type=as_str(r[0]),
                blocks=as_int(r[1]),
                with_bbox=as_int(r[2]),
                leaves=as_int(r[3]),
            )
            for r in type_rows
        ],
        page_block_counts=[
            PageBlockCount(page=as_int(r[0]) if r[0] is not None else None, blocks=as_int(r[1]))
            for r in page_rows
        ],
    )


def get_page_layout(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    doc_id: int,
    page: int,
    as_of: datetime,
) -> LayoutResponse | None:
    """版面还原视图的数据源。`bbox_normalized=True` 恒为真——`parse/
    textin.py::_bbox` 写入前已经按页面尺寸归一化到 [0,1]，数据库没有存
    页面物理尺寸，前端不该自行猜测坐标系（裁决 4：不做 PDF 底图，画布
    长宽比是前端的假设值，与这里的归一化坐标无关）。
    """
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        doc_row = conn.execute(
            "SELECT page_count FROM asof.document"
            " WHERE doc_id = %(doc_id)s AND owner_tenant IS NULL AND owner_user IS NULL",
            {"doc_id": doc_id},
        ).fetchone()
        if doc_row is None:
            return None
        rows = conn.execute(
            _PAGE_LAYOUT_SQL,
            {"doc_id": doc_id, "page": page, "preview_chars": _PREVIEW_CHARS},
        ).fetchall()

    blocks: list[LayoutBlock] = []
    bbox_missing_count = 0
    for r in rows:
        bbox, malformed = as_bbox(r[6])
        if bbox is None:
            bbox_missing_count += 1
        blocks.append(
            LayoutBlock(
                block_id=as_int(r[0]),
                parent_block_id=as_int(r[1]) if r[1] is not None else None,
                block_type=as_str(r[2]),
                section_path=as_str(r[3]),
                ordinal=as_int(r[4]),
                page=as_int(r[5]) if r[5] is not None else None,
                bbox=bbox,
                bbox_malformed=malformed,
                is_leaf=as_bool(r[7]),
                char_len=as_int(r[8]) if r[8] is not None else None,
                parse_confidence=as_optional_float(r[9]),
                preview=as_str(r[10]),
                has_desc=as_bool(r[11]),
                has_table_html=as_bool(r[12]),
            )
        )

    return LayoutResponse(
        doc_id=doc_id,
        page=page,
        page_count=as_int(doc_row[0]) if doc_row[0] is not None else None,
        bbox_normalized=True,
        bbox_missing_count=bbox_missing_count,
        blocks=blocks,
    )


__all__ = ("get_document", "get_page_layout", "list_documents")
