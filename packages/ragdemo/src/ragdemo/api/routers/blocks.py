"""GET /api/documents/{doc_id}/blocks、/tree、/api/blocks/{block_id}。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from ragdemo.api.deps import AsOf, ConnDep
from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.queries.blocks import get_block, get_tree, list_blocks
from ragdemo.api.schemas import BlockDetail, BlocksResponse, TreeResponse

router = APIRouter(tags=["blocks"])


@router.get("/documents/{doc_id}/blocks", response_model=BlocksResponse)
def get_document_blocks(
    as_of: AsOf,
    conn: ConnDep,
    doc_id: Annotated[int, Path()],
) -> BlocksResponse:
    blocks = list_blocks(conn, doc_id=doc_id, as_of=as_of)
    if blocks is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"文档 {doc_id} 在这个 as_of 下不存在或不可见")
    return BlocksResponse(doc_id=doc_id, as_of=as_of.isoformat(), blocks=blocks)


@router.get("/documents/{doc_id}/tree", response_model=TreeResponse)
def get_document_tree(
    as_of: AsOf,
    conn: ConnDep,
    doc_id: Annotated[int, Path()],
) -> TreeResponse:
    tree = get_tree(conn, doc_id=doc_id, as_of=as_of)
    if tree is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"文档 {doc_id} 在这个 as_of 下不存在或不可见")
    return tree


@router.get("/blocks/{block_id}", response_model=BlockDetail)
def get_block_detail(
    as_of: AsOf,
    conn: ConnDep,
    block_id: Annotated[int, Path()],
) -> BlockDetail:
    block = get_block(conn, block_id=block_id, as_of=as_of)
    if block is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"块 {block_id} 在这个 as_of 下不存在或不可见")
    return block
