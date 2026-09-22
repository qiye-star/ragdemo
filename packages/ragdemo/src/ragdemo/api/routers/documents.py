"""GET /api/documents 系——文档列表/详情/版面还原。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.queries.documents import get_document, get_page_layout, list_documents
from ragdemo.api.schemas import (
    DocumentDetail,
    DocumentListResponse,
    DocumentSummary,
    LayoutResponse,
)

router = APIRouter(tags=["documents"])


@router.get("/documents", response_model=DocumentListResponse)
def get_documents(
    as_of: AsOf,
    conn: ConnDep,
    entity_id: Annotated[str | None, Query()] = None,
    doc_type: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    documents: list[DocumentSummary] = list_documents(
        conn,
        as_of=as_of,
        entity_id=entity_id,
        doc_type=doc_type,
        source=source,
        limit=limit,
        offset=offset,
    )
    return DocumentListResponse(as_of=as_of.isoformat(), documents=documents)


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
def get_document_detail(
    as_of: AsOf,
    conn: ConnDep,
    doc_id: Annotated[int, Path()],
) -> DocumentDetail:
    detail = get_document(conn, doc_id=doc_id, as_of=as_of)
    if detail is None:
        # 不可见与不存在刻意报同一个 404——可区分本身就是一个存在性侧信道。
        raise ApiError(404, ErrorCode.NOT_FOUND, f"文档 {doc_id} 在这个 as_of 下不存在或不可见")
    return detail


@router.get("/documents/{doc_id}/pages/{page}/layout", response_model=LayoutResponse)
def get_document_page_layout(
    as_of: AsOf,
    conn: ConnDep,
    doc_id: Annotated[int, Path()],
    page: Annotated[int, Path(ge=1)],
) -> LayoutResponse:
    layout = get_page_layout(conn, doc_id=doc_id, page=page, as_of=as_of)
    if layout is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"文档 {doc_id} 在这个 as_of 下不存在或不可见")
    return layout
