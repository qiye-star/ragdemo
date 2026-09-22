"""GET /api/retrieval/search —— 检索诊断：BM25/向量/融合/重排四段排名并排。

只读 GET；查询本身会调用嵌入与（默认关闭时跳过的）重排——两者默认都是
Mock，不产生任何外部网络调用或计费（web-diagnostic-ui 计划裁决 5）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ragdemo.api.deps import AsOf, ConnDep, SettingsDep
from ragdemo.api.queries.retrieval import run_search
from ragdemo.api.schemas import RetrievalSearchResponse

router = APIRouter(tags=["retrieval"])


@router.get("/retrieval/search", response_model=RetrievalSearchResponse)
def get_retrieval_search(
    as_of: AsOf,
    conn: ConnDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=512, description="检索查询文本")],
    top_k: Annotated[int, Query(ge=1, le=50)] = 10,
    candidate_k: Annotated[int, Query(ge=1, le=200)] = 50,
    entity_id: Annotated[list[str] | None, Query()] = None,
    doc_type: Annotated[list[str] | None, Query()] = None,
    rerank: Annotated[bool, Query()] = True,
    rewrite: Annotated[bool, Query()] = False,
) -> RetrievalSearchResponse:
    return run_search(
        conn,
        settings,
        q=q,
        as_of=as_of,
        top_k=top_k,
        candidate_k=candidate_k,
        entity_ids=entity_id,
        doc_types=doc_type,
        rerank=rerank,
        rewrite=rewrite,
    )
