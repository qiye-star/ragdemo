"""检索编排：过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开。

查询走 asof 视图（正确性）+ 显式时点谓词（可下推），见 docs/06-retrieval.md §3.4。

`apply_scan_settings()` 设置的 pgvector 扫描参数用 `SET LOCAL` 语义，必须与
实际发起向量查询的语句共享同一个事务（`vector.py` 的 `apply_scan_settings`
文档字符串）；`bm25_search` / `vector_search` / `expand_to_evidence` 各自内部
也都用 `as_of_session` 包了一层——这里在编排层再包一层是必要的（否则
`apply_scan_settings` 无处依附），而不是重复劳动：psycopg 对已经处于事务中的
连接再次进入 `conn.transaction()` 会自动退化为 SAVEPOINT，两层嵌套不冲突，
内层 `as_of_session` 设置的 GUC 在其 SAVOEPOINT 释放后仍在外层事务中生效。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence

import psycopg

from ragdemo.embed.base import Embedder
from ragdemo.retrieval.expand import expand_to_evidence
from ragdemo.retrieval.fusion import FusedHit, weighted_rrf
from ragdemo.retrieval.lexical import bm25_search
from ragdemo.retrieval.rerank import Reranker, RerankOutcome, rerank_or_degrade
from ragdemo.retrieval.rewrite import expand_synonyms
from ragdemo.retrieval.types import RetrievalRequest, RetrievalResult, RetrievalStats
from ragdemo.retrieval.vector import apply_scan_settings, vector_search
from ragdemo.retrieval.vector_index import (
    ChromaVectorIndex,
    VectorIndex,
    chroma_client_from_env,
)
from ragdemo_core.db.session import as_of_session

logger = logging.getLogger(__name__)

# 改写变体（同义词替换后的查询）相对原查询的权重折扣：变体只是对原查询的近似
# 改写，不应该跟原查询在融合排序里平权竞争，降权是为了避免改写引入的噪声盖过
# 主查询的信号。
_VARIANT_WEIGHT = 0.5


class RetrievalService:
    """编排层：五个阶段串成一次查询，唯一对外入口是 `search()`。"""

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        reranker: Reranker,
        *,
        synonyms: Mapping[str, list[str]] | None = None,
        index: VectorIndex | None = None,
    ) -> None:
        """`index` 是向量候选生成器（ADR-0009）。

        省略时 `vector_search` 退回 `PgVectorIndex`，走库内 pgvector HNSW。
        要让向量召回真正走 Chroma，**必须**在这里把 `ChromaVectorIndex` 传进来——
        生产路径用 `chroma_backed_service()`，它从环境变量构造客户端。

        留一个可省略的参数而不是在这里直接 `chroma_client_from_env()`，
        是因为那样会让每次构造 `RetrievalService` 都强制依赖一个在跑的 Chroma
        实例，单元测试与离线评测都会被拖下水；而 pgvector 退路本来就是
        ADR-0009 后果 4 要求保留的对照组。
        """
        self.conn = conn
        self.embedder = embedder
        self.reranker = reranker
        self.synonyms = dict(synonyms or {})
        self.index = index

    def search(self, req: RetrievalRequest) -> RetrievalResult:
        """过滤 → 双路召回 → 加权 RRF → 重排 → 父子块展开，返回证据与统计。"""
        cfg = req.config
        run_id = uuid.uuid4().hex
        started = time.perf_counter()

        with as_of_session(self.conn, req.as_of, tenant=req.tenant, user=req.user) as conn:
            # 必须与下面的 vector_search 共享同一个事务/保存点，见 vector.py 里
            # apply_scan_settings 的文档字符串。
            apply_scan_settings(conn, cfg)

            queries = (
                expand_synonyms(req.query, self.synonyms) if cfg.rewrite_enabled else [req.query]
            )

            t0 = time.perf_counter()
            bm25 = bm25_search(conn, req)
            ms_bm25 = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            qvec = self.embedder.embed([req.query])[0]
            vec = vector_search(conn, req, qvec, index=self.index)
            ms_vec = (time.perf_counter() - t0) * 1000

            fused = weighted_rrf(
                bm25,
                vec,
                w_bm25=cfg.w_bm25,
                w_vec=cfg.w_vec,
                rrf_k=cfg.rrf_k,
                limit=cfg.fusion_k,
            )

            # 查询改写：每个同义词变体各自召回、各自融合（权重打折），再并入主结果。
            for variant in queries[1:]:
                variant_req = _with_query(req, variant)
                variant_vec = self.embedder.embed([variant])[0]
                variant_fused = weighted_rrf(
                    bm25_search(conn, variant_req),
                    vector_search(conn, variant_req, variant_vec, index=self.index),
                    w_bm25=cfg.w_bm25 * _VARIANT_WEIGHT,
                    w_vec=cfg.w_vec * _VARIANT_WEIGHT,
                    rrf_k=cfg.rrf_k,
                    limit=cfg.fusion_k,
                )
                fused = _merge(fused, variant_fused, cfg.fusion_k)

            ms_rerank = 0.0
            if cfg.rerank_enabled and fused:
                contents = _leaf_contents(conn, [f.block_id for f in fused])
                t0 = time.perf_counter()
                outcome = rerank_or_degrade(self.reranker, req.query, fused, contents, cfg.top_k)
                ms_rerank = (time.perf_counter() - t0) * 1000
            else:
                # 重排关闭（或没有融合候选）时直接截断 RRF 顺序，不是降级——
                # 降级特指「尝试过重排但失败了」，两者的运维含义不同。
                head = fused[: cfg.top_k]
                outcome = RerankOutcome(
                    order=[f.block_id for f in head],
                    scores={f.block_id: f.score for f in head},
                    degraded=False,
                )

            blocks = expand_to_evidence(
                conn,
                outcome.order,
                outcome.scores,
                reranked=cfg.rerank_enabled and not outcome.degraded,
                top_k=cfg.top_k,
                as_of=req.as_of,
                tenant=req.tenant,
                user=req.user,
            )

        stats = RetrievalStats(
            bm25_hits=len(bm25),
            vec_hits=len(vec),
            after_fusion=len(fused),
            after_rerank=len(outcome.order),
            ms_bm25=ms_bm25,
            ms_vec=ms_vec,
            ms_rerank=ms_rerank,
            ms_total=(time.perf_counter() - started) * 1000,
            degraded=outcome.degraded,
        )
        logger.info(
            "retrieval",
            extra={
                "run_id": run_id,
                "as_of": req.as_of.isoformat(),
                "entity_id": req.entity_ids,
                "query_len": len(req.query),
                "stats": stats,
            },
        )
        return RetrievalResult(blocks=blocks, stats=stats)


def _with_query(req: RetrievalRequest, query: str) -> RetrievalRequest:
    """用改写变体替换 query，其余字段原样复用。"""
    return RetrievalRequest(
        query=query,
        as_of=req.as_of,
        entity_ids=req.entity_ids,
        doc_types=req.doc_types,
        published_after=req.published_after,
        tenant=req.tenant,
        user=req.user,
        config=req.config,
    )


def _merge(primary: Sequence[FusedHit], extra: Sequence[FusedHit], limit: int) -> list[FusedHit]:
    """把改写变体的融合结果并入主结果：同一 block_id 分数相加，排名取先出现的一路。"""
    scores: dict[int, float] = {}
    ranks: dict[int, tuple[int | None, int | None]] = {}
    for hit in (*primary, *extra):
        scores[hit.block_id] = scores.get(hit.block_id, 0.0) + hit.score
        ranks.setdefault(hit.block_id, (hit.bm25_rank, hit.vec_rank))
    merged = [
        FusedHit(
            block_id=block_id,
            score=score,
            bm25_rank=ranks[block_id][0],
            vec_rank=ranks[block_id][1],
        )
        for block_id, score in scores.items()
    ]
    merged.sort(key=lambda f: (-f.score, f.block_id))
    return merged[:limit]


def chroma_backed_service(
    conn: psycopg.Connection,
    embedder: Embedder,
    reranker: Reranker,
    *,
    synonyms: Mapping[str, list[str]] | None = None,
    owner_user: str | None = None,
) -> RetrievalService:
    """生产路径的构造入口：向量召回走 Chroma（ADR-0009）。

    客户端由 `chroma_client_from_env()` 按环境变量构造，并拒绝任何指向公网的
    地址——原始文档与实体财务数据不得出境（`CLAUDE.md` §0）。

    `owner_user` 非空时会落到该用户专属的 collection：用户上传材料私有隔离
    靠的是物理分库而不是查询条件（ADR-0009 边界）。不传就是公共检索空间。

    直接 `RetrievalService(...)` 而不传 `index` 得到的是 pgvector 退路，
    那是对照组不是生产配置——需要 Chroma 就用这个函数。
    """
    index = ChromaVectorIndex(chroma_client_from_env(), owner_user=owner_user)
    return RetrievalService(conn, embedder, reranker, synonyms=synonyms, index=index)


def _leaf_contents(conn: psycopg.Connection, block_ids: list[int]) -> dict[int, str]:
    """按 block_id 取叶子块正文，供重排使用（重排要叶子块，不要父块，见 rerank.py）。"""
    if not block_ids:
        return {}
    rows = conn.execute(
        "SELECT block_id, content FROM asof.doc_block WHERE block_id = ANY(%s)",
        (block_ids,),
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}
