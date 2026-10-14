"""加权 RRF（06 §4）。用排名不用分数——BM25 分数无界，不能直接加权。"""

from __future__ import annotations

from ragdemo.retrieval.fusion import FusedHit, guarantee_vector_top, weighted_rrf
from ragdemo.retrieval.lexical import RankedHit

K = 60


def _hits(*ids: int) -> list[RankedHit]:
    return [RankedHit(block_id=b, rank=i + 1, raw_score=0.0) for i, b in enumerate(ids)]


def test_block_in_both_lists_scores_higher_than_either_alone() -> None:
    fused = weighted_rrf(_hits(1, 2), _hits(1, 3), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    by_id = {f.block_id: f.score for f in fused}
    assert by_id[1] > by_id[2]
    assert by_id[1] > by_id[3]


def test_missing_side_contributes_zero_not_a_penalty() -> None:
    """某一路未召回该块时，该项贡献 0（06 §4.1）。"""
    fused = weighted_rrf(_hits(1), [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert fused[0].score == 0.6 / (K + 1)
    assert fused[0].vec_rank is None


def test_weights_shift_the_ordering() -> None:
    bm25, vec = _hits(1, 2), _hits(2, 1)
    bm25_heavy = weighted_rrf(bm25, vec, w_bm25=0.9, w_vec=0.1, rrf_k=K, limit=10)
    vec_heavy = weighted_rrf(bm25, vec, w_bm25=0.1, w_vec=0.9, rrf_k=K, limit=10)
    assert bm25_heavy[0].block_id == 1
    assert vec_heavy[0].block_id == 2


def test_results_are_sorted_descending_by_score() -> None:
    fused = weighted_rrf(_hits(1, 2, 3), _hits(3, 2, 1), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert [f.score for f in fused] == sorted((f.score for f in fused), reverse=True)


def test_limit_truncates() -> None:
    fused = weighted_rrf(_hits(1, 2, 3), [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=2)
    assert len(fused) == 2


def test_empty_inputs_give_empty_output() -> None:
    assert weighted_rrf([], [], w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10) == []


def test_ranks_are_preserved_for_diagnostics() -> None:
    """stats 与排查「为什么没找到」都要看两路各自的排名。"""
    fused = weighted_rrf(_hits(1), _hits(1), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    assert (fused[0].bm25_rank, fused[0].vec_rank) == (1, 1)


def test_score_formula_matches_the_spec() -> None:
    fused = weighted_rrf(_hits(7), _hits(9, 7), w_bm25=0.6, w_vec=0.4, rrf_k=K, limit=10)
    seven = next(f for f in fused if f.block_id == 7)
    assert seven.score == 0.6 / (K + 1) + 0.4 / (K + 2)


# ---- 向量保底进重排（guarantee_vector_top） ----
# 融合里 BM25 权重 0.6，只被向量命中的块得分只相当于 BM25 的 1.5r+30 名，向量第 10 名以后
# 的块会被挤出 fusion_k，根本到不了重排（表格题检索失败分析.md 机制 2）。保底规则：向量前 n
# 名一定进重排候选，总数仍不超过 limit（= fusion_k，重排文档数不变）。


def _fused(*ids: int) -> list[FusedHit]:
    return [
        FusedHit(block_id=b, score=1.0 / (i + 1), bm25_rank=i + 1, vec_rank=None)
        for i, b in enumerate(ids)
    ]


def _guarantee(fused: list[FusedHit], vec: list[RankedHit], n: int, limit: int) -> list[FusedHit]:
    return guarantee_vector_top(fused, [], vec, n=n, limit=limit, w_bm25=0.6, w_vec=0.4, rrf_k=K)


def test_guarantee_zero_is_plain_truncation() -> None:
    fused = _fused(1, 2, 3)
    assert _guarantee(fused, _hits(9, 8), n=0, limit=2) == fused[:2]


def test_guarantee_adds_vector_top_missing_from_fusion_and_keeps_size() -> None:
    fused = _fused(1, 2, 3, 4)
    out = _guarantee(fused, _hits(1, 9), n=2, limit=4)
    assert [f.block_id for f in out] == [1, 2, 3, 9]


def test_guarantee_only_reaches_top_n_of_vector() -> None:
    out = _guarantee(_fused(1, 2, 3), _hits(7, 8, 9), n=2, limit=3)
    ids = [f.block_id for f in out]
    assert 7 in ids and 8 in ids and 9 not in ids
    assert len(ids) == 3


def test_guarantee_never_drops_a_protected_block_already_in_fusion_tail() -> None:
    # 9 在融合最后一名，同时是向量第 1 名；为 8 腾位置时被挤掉的必须是 3，不是 9。
    out = _guarantee(_fused(1, 2, 3, 9), _hits(9, 8), n=2, limit=4)
    ids = [f.block_id for f in out]
    assert ids == [1, 2, 9, 8]


def test_guarantee_does_not_pad_when_union_is_small() -> None:
    out = _guarantee(_fused(1), _hits(1, 2), n=2, limit=50)
    assert [f.block_id for f in out] == [1, 2]


def test_guarantee_appended_hit_has_rrf_score_and_ranks() -> None:
    out = guarantee_vector_top(
        _fused(1), _hits(5, 7), _hits(3, 7), n=2, limit=3, w_bm25=0.6, w_vec=0.4, rrf_k=K
    )
    seven = next(f for f in out if f.block_id == 7)
    assert (seven.bm25_rank, seven.vec_rank) == (2, 2)
    assert seven.score == 0.6 / (K + 2) + 0.4 / (K + 2)


def test_guarantee_has_no_duplicates() -> None:
    out = _guarantee(_fused(1, 2, 3), _hits(2, 1, 3), n=3, limit=3)
    ids = [f.block_id for f in out]
    assert sorted(ids) == [1, 2, 3] and len(set(ids)) == len(ids)
