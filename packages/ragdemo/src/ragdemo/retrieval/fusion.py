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


def guarantee_vector_top(
    fused: Sequence[FusedHit],
    bm25: Sequence[RankedHit],
    vec: Sequence[RankedHit],
    *,
    n: int,
    limit: int,
    w_bm25: float,
    w_vec: float,
    rrf_k: int,
) -> list[FusedHit]:
    """重排候选 = 融合结果 + 向量一路前 n 名保底，总数不超过 `limit`。

    BM25 权重高时，只被向量命中的块会被挤出融合前 `limit`，根本到不了重排
    （表格题检索失败分析.md 机制 2）。这里保证向量前 n 名一定在候选里：
    已在融合结果里的保持原位；不在的追加到末尾（按向量名次），为它们腾位置时
    从融合尾部挤掉**非保底**的块。`n=0` 等于直接截断，与不加保底完全一致。

    追加块的分数按 `weighted_rrf` 同一公式算（该块在 BM25 一路有排名就计入），
    只用于降级时的回退顺序与诊断，不影响重排本身。
    """
    if n <= 0:
        return list(fused)[:limit]
    protected = [h for h in sorted(vec, key=lambda h: h.rank) if h.rank <= n]
    protected_ids = {h.block_id for h in protected}
    in_fused = {f.block_id for f in fused}
    missing = [h for h in protected if h.block_id not in in_fused]

    n_protected_in_fused = len(protected_ids & in_fused)
    free_slots = max(limit - len(missing) - n_protected_in_fused, 0)
    kept: list[FusedHit] = []
    for f in fused:
        if f.block_id in protected_ids:
            kept.append(f)
        elif free_slots > 0:
            kept.append(f)
            free_slots -= 1

    bm25_ranks = {h.block_id: h.rank for h in bm25}
    for h in missing:
        b_rank = bm25_ranks.get(h.block_id)
        kept.append(
            FusedHit(
                block_id=h.block_id,
                score=(w_bm25 / (rrf_k + b_rank) if b_rank is not None else 0.0)
                + w_vec / (rrf_k + h.rank),
                bm25_rank=b_rank,
                vec_rank=h.rank,
            )
        )
    return kept[:limit]
