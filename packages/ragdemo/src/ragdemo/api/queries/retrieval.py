"""检索诊断查询：把 `RetrievalService` 的输出重新整理成「每行一个候选块，
BM25/向量/融合/重排/最终六列并排」的形状——ADR-0010 立项理由之一
（"要判断融合排名合不合理，需要同一查询下 20 行 × 6 列并排比较"）。

**只读，只碰 `asof.doc_block` / `asof.document`。** `tests/api/test_sql_guards.py`
只扫描本目录（`api/queries/*.py`）里手写的 SQL——这条约束覆盖本文件自己的两条
查询（都只碰 asof 视图），但**不覆盖**本文件复用的 `ragdemo.retrieval.*` 内部
查询（`bm25_search`/`vector_search`/`expand_to_evidence` 等）。那些查询本来就
只查 asof 视图（`retrieval/expand.py` 模块文档已经说明），只是不在这条源码扫描
守卫的射程内，这里显式写明这件事，不留给读者自己发现。

**事务与超时。** `RetrievalService.search()` 内部用 `as_of_session` 开一个事务
（进而是嵌套 `conn.transaction()`）。本模块在外面再包一层 `conn.transaction()`
设置更长的 `statement_timeout`——嵌套 `transaction()` 在 psycopg3 里退化成
SAVEPOINT，本模块设置的 `SET LOCAL` 在外层事务范围内持续有效，
`service.py` 自己的模块 docstring 用同样的论证解释过 `apply_scan_settings`
为什么能这样做。默认连接级 `statement_timeout=5000`（`deps.py::get_conn`）
对 ParadeDB BM25 + pgvector HNSW 同时跑的检索请求偏紧，这里放宽到 15000ms。

**idle-in-transaction 的代价。** 嵌入与重排都是网络调用，被 `as_of_session`
的事务包住意味着这段时间数据库连接处于 idle-in-transaction。默认模型是
Mock（微秒级），可以忽略；一旦通过 `RAGDEMO_API_RETRIEVAL_MODELS=siliconflow`
切到真实模型，这会是 100–2000ms 的 idle-in-transaction——这是已知的、
接受的代价（web-diagnostic-ui 计划裁决 5「建而不接线」正是为了不让
默认路径承担它）。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime

import psycopg

from ragdemo.adapters.siliconflow import client_from_config
from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.schemas import (
    CoverageInfo,
    ModelsInfo,
    RetrievalRow,
    RetrievalSearchResponse,
    RetrievalStatsOut,
)
from ragdemo.api.serialize import as_int, as_optional_int, as_str
from ragdemo.api.settings import ApiSettings
from ragdemo.config import load_config
from ragdemo.embed.base import Embedder
from ragdemo.embed.mock import MockEmbedder
from ragdemo.embed.siliconflow import SiliconFlowEmbedder
from ragdemo.retrieval.rerank import MockReranker, Reranker
from ragdemo.retrieval.rerank_siliconflow import SiliconFlowReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest, RetrievalTrace
from ragdemo_core.db.session import as_of_session

_PREVIEW_CHARS = 120
_STATEMENT_TIMEOUT_MS = 15000

_SQL_CANDIDATE_METADATA = """
SELECT b.block_id, b.doc_id, d.title, b.section_path, b.page,
       left(b.content, %(preview_chars)s) AS preview
  FROM asof.doc_block b
  JOIN asof.document d ON d.doc_id = b.doc_id
 WHERE b.block_id = ANY(%(block_ids)s)
   AND b.owner_tenant IS NULL AND b.owner_user IS NULL
"""

_SQL_COVERAGE = """
SELECT count(*) FILTER (WHERE is_leaf),
       count(*) FILTER (WHERE is_leaf AND embedding IS NOT NULL)
  FROM asof.doc_block
 WHERE owner_tenant IS NULL AND owner_user IS NULL
"""

_SQL_EMBEDDING_VERSIONS = """
SELECT DISTINCT embedding_version
  FROM asof.doc_block
 WHERE embedding IS NOT NULL AND embedding_version IS NOT NULL
   AND owner_tenant IS NULL AND owner_user IS NULL
"""

_CACHE_CAPACITY = 64
_cache: OrderedDict[tuple[object, ...], RetrievalSearchResponse] = OrderedDict()


def clear_cache() -> None:
    """测试用：模块级缓存跨请求（乃至跨测试用例）持续存在，同一个
    (query, as_of, ...) 元组第二次调用不该再触发任何数据库/网络调用——
    但也不该让一个测试的缓存悄悄影响另一个用了相同参数的测试。"""
    _cache.clear()


def _cache_key(
    *,
    q: str,
    as_of: datetime,
    top_k: int,
    candidate_k: int,
    entity_ids: Sequence[str] | None,
    doc_types: Sequence[str] | None,
    rerank: bool,
    rewrite: bool,
    models_mode: str,
) -> tuple[object, ...]:
    return (
        q,
        as_of.isoformat(),
        top_k,
        candidate_k,
        tuple(entity_ids or ()),
        tuple(doc_types or ()),
        rerank,
        rewrite,
        models_mode,
    )


def _build_models(settings: ApiSettings) -> tuple[Embedder, Reranker]:
    if settings.retrieval_models == "mock":
        return MockEmbedder(), MockReranker()

    # "siliconflow"：from_env() 已经把这个值校验成只能是这两者之一。
    config = load_config()
    base_url = config.require_siliconflow_base_url()
    # 嵌入与重排是同一个供应商——共用一个 HttpClient 实例，令牌桶/日配额
    # 因此正确地按供应商而不是按操作类型计数（http.py::HttpClient 的类
    # docstring："一个供应商一个实例"）。
    http = client_from_config(base_url)
    embedder = SiliconFlowEmbedder(http, model=config.siliconflow_embed_model)
    reranker = SiliconFlowReranker(http, model=config.siliconflow_rerank_model)
    return embedder, reranker


def _vector_path_is_meaningful(active_model: str, corpus_versions: Sequence[str]) -> bool:
    """当前 embedder 是 Mock，或语料里没有任何一个块的 embedding_version
    与它匹配时，向量列的排名是伪随机数，不是真排名——两种情况都判 False。

    `embedding_version` 写入时就是 `embedder.model` 本身（`embed/batch.py`），
    没有任何前缀约定，这里按同样的字面值比较。
    """
    if active_model == MockEmbedder.model:
        return False
    if not corpus_versions:
        return False
    return active_model in corpus_versions


def _corpus_embedding_versions(conn: psycopg.Connection, as_of: datetime) -> list[str]:
    with as_of_session(conn, as_of):
        rows = conn.execute(_SQL_EMBEDDING_VERSIONS).fetchall()
    return sorted({as_str(r[0]) for r in rows})


def _coverage(conn: psycopg.Connection, as_of: datetime) -> CoverageInfo:
    with as_of_session(conn, as_of):
        row = conn.execute(_SQL_COVERAGE).fetchone()
    if row is None:
        raise RuntimeError("SELECT count(*) FILTER (...) 没有返回任何行，这不应该发生")
    return CoverageInfo(leaf_blocks=as_int(row[0]), with_embedding=as_int(row[1]))


class _CandidateMeta:
    __slots__ = ("doc_id", "doc_title", "page", "preview", "section_path")

    def __init__(
        self, doc_id: int, doc_title: str, section_path: str, page: int | None, preview: str
    ) -> None:
        self.doc_id = doc_id
        self.doc_title = doc_title
        self.section_path = section_path
        self.page = page
        self.preview = preview


def _candidate_metadata(
    conn: psycopg.Connection, as_of: datetime, block_ids: Sequence[int]
) -> dict[int, _CandidateMeta]:
    if not block_ids:
        return {}
    with as_of_session(conn, as_of):
        rows = conn.execute(
            _SQL_CANDIDATE_METADATA,
            {"block_ids": list(block_ids), "preview_chars": _PREVIEW_CHARS},
        ).fetchall()
    return {
        as_int(r[0]): _CandidateMeta(
            doc_id=as_int(r[1]),
            doc_title=as_str(r[2]),
            section_path=as_str(r[3]),
            page=as_optional_int(r[4]),
            preview=as_str(r[5]),
        )
        for r in rows
    }


def _rows_from_trace(
    trace: RetrievalTrace, metadata: dict[int, _CandidateMeta]
) -> list[RetrievalRow]:
    final_position = {block_id: i + 1 for i, block_id in enumerate(trace.final_order)}
    bm25_by_id = {s.block_id: s for s in trace.bm25}
    vec_by_id = {s.block_id: s for s in trace.vec}
    fused_by_id = {s.block_id: s for s in trace.fused}
    rerank_by_id = {s.block_id: s for s in trace.rerank}
    all_ids = {*bm25_by_id, *vec_by_id, *fused_by_id, *rerank_by_id}

    rows: list[RetrievalRow] = []
    for block_id in all_ids:
        meta = metadata.get(block_id)
        if meta is None:
            # 时点/权限收窄导致这个块此刻不可见——不偷渡任何信息，直接丢行，
            # 不是「显示但留空」。理论上不该发生（各阶段的候选本来就来自
            # 同一个可见性谓词），防御性地处理而不是假设它不会发生。
            continue
        bm25 = bm25_by_id.get(block_id)
        vec = vec_by_id.get(block_id)
        fused = fused_by_id.get(block_id)
        rerank = rerank_by_id.get(block_id)
        rows.append(
            RetrievalRow(
                block_id=block_id,
                doc_id=meta.doc_id,
                doc_title=meta.doc_title,
                section_path=meta.section_path,
                page=meta.page,
                preview=meta.preview,
                bm25_rank=bm25.rank if bm25 else None,
                bm25_score=bm25.score if bm25 else None,
                vec_rank=vec.rank if vec else None,
                vec_score=vec.score if vec else None,
                fused_rank=fused.rank if fused else None,
                fused_score=fused.score if fused else None,
                rerank_rank=rerank.rank if rerank else None,
                rerank_score=rerank.score if rerank else None,
                final_position=final_position.get(block_id),
            )
        )

    def sort_key(r: RetrievalRow) -> tuple[bool, int, bool, int]:
        return (
            r.final_position is None,
            r.final_position or 0,
            r.fused_rank is None,
            r.fused_rank or 0,
        )

    rows.sort(key=sort_key)
    return rows


def run_search(
    conn: psycopg.Connection,
    settings: ApiSettings,
    *,
    q: str,
    as_of: datetime,
    top_k: int,
    candidate_k: int,
    entity_ids: Sequence[str] | None,
    doc_types: Sequence[str] | None,
    rerank: bool,
    rewrite: bool,
) -> RetrievalSearchResponse:
    key = _cache_key(
        q=q,
        as_of=as_of,
        top_k=top_k,
        candidate_k=candidate_k,
        entity_ids=entity_ids,
        doc_types=doc_types,
        rerank=rerank,
        rewrite=rewrite,
        models_mode=settings.retrieval_models,
    )
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached

    try:
        config = RetrievalConfig(
            candidate_k=candidate_k,
            fusion_k=candidate_k,
            top_k=top_k,
            rerank_enabled=rerank,
            rewrite_enabled=rewrite,
        )
        req = RetrievalRequest(
            query=q,
            as_of=as_of,
            entity_ids=list(entity_ids) if entity_ids else None,
            doc_types=list(doc_types) if doc_types else None,
            config=config,
        )
    except ValueError as exc:
        raise ApiError(422, ErrorCode.INVALID_PARAM, str(exc)) from exc

    embedder, reranker = _build_models(settings)

    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}'")
        service = RetrievalService(conn, embedder, reranker)
        result = service.search(req, trace=True)
        corpus_versions = _corpus_embedding_versions(conn, as_of)
        coverage = _coverage(conn, as_of)

        trace = result.trace
        assert trace is not None, "run_search 总是要求 trace=True"
        candidate_ids = {
            *(s.block_id for s in trace.bm25),
            *(s.block_id for s in trace.vec),
            *(s.block_id for s in trace.fused),
            *(s.block_id for s in trace.rerank),
        }
        metadata = _candidate_metadata(conn, as_of, sorted(candidate_ids))

    rows = _rows_from_trace(trace, metadata)[:candidate_k]

    response = RetrievalSearchResponse(
        query=q,
        as_of=as_of.isoformat(),
        models=ModelsInfo(
            embedder=trace.embedder_model,
            reranker=trace.reranker_model,
            embedder_is_mock=trace.embedder_model == MockEmbedder.model,
            reranker_is_mock=trace.reranker_model == MockReranker.model,
            corpus_embedding_versions=corpus_versions,
            vector_path_is_meaningful=_vector_path_is_meaningful(
                trace.embedder_model, corpus_versions
            ),
        ),
        stats=RetrievalStatsOut(
            bm25_hits=result.stats.bm25_hits,
            vec_hits=result.stats.vec_hits,
            after_fusion=result.stats.after_fusion,
            after_rerank=result.stats.after_rerank,
            ms_bm25=result.stats.ms_bm25,
            ms_vec=result.stats.ms_vec,
            ms_rerank=result.stats.ms_rerank,
            ms_total=result.stats.ms_total,
            degraded=result.stats.degraded,
            rerank_attempted=trace.rerank_attempted,
        ),
        coverage=coverage,
        rows=rows,
    )

    _cache[key] = response
    _cache.move_to_end(key)
    if len(_cache) > _CACHE_CAPACITY:
        _cache.popitem(last=False)
    return response
