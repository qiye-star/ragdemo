"""加权 RRF（06 §4）。用排名不用分数——BM25 分数无界，不能直接加权。"""
from __future__ import annotations

from ragdemo.retrieval.fusion import weighted_rrf
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
