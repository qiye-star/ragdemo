"""加权 Reciprocal Rank Fusion。

原简报同时写了「BM25 权重 0.6 / 向量 0.4」与「RRF 合并」，这是两种互斥的融合：
加权线性融合用分数（BM25 分数无界，必须先归一化，而归一化对异常值敏感），
RRF 只用排名（天然无量纲）。统一为加权 RRF——权重作用在 RRF 项上，
既保留「BM25 更重要」的意图，又避免分数尺度问题（docs/06-retrieval.md §4.1）。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ragdemo.retrieval.lexical import RankedHit


@dataclass(frozen=True)
class FusedHit:
    block_id: int
    score: float
    bm25_rank: int | None
    vec_rank: int | None


def weighted_rrf(
    bm25: Sequence[RankedHit],
    vec: Sequence[RankedHit],
    *,
    w_bm25: float,
    w_vec: float,
    rrf_k: int,
    limit: int,
) -> list[FusedHit]:
    """score(d) = w_bm25/(k + rank_bm25) + w_vec/(k + rank_vec)，缺席的一路贡献 0。"""
    bm25_ranks = {h.block_id: h.rank for h in bm25}
    vec_ranks = {h.block_id: h.rank for h in vec}

    fused = [
        FusedHit(
            block_id=block_id,
            score=(
                (w_bm25 / (rrf_k + bm25_ranks[block_id]) if block_id in bm25_ranks else 0.0)
                + (w_vec / (rrf_k + vec_ranks[block_id]) if block_id in vec_ranks else 0.0)
            ),
            bm25_rank=bm25_ranks.get(block_id),
            vec_rank=vec_ranks.get(block_id),
        )
        for block_id in {*bm25_ranks, *vec_ranks}
    ]
    fused.sort(key=lambda f: (-f.score, f.block_id))
    return fused[:limit]
