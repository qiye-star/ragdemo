"""检索指标：recall@k 与 MRR 的定义必须和 06 §9.1 一致。"""
from __future__ import annotations

import pytest

from ragdemo.evals.retrieval_metrics import mrr_at_k, recall_at_k


def test_recall_is_binary_per_question() -> None:
    """top-k 中包含至少一个 gold 即算命中——不是 gold 的覆盖比例。"""
    assert recall_at_k([1, 2, 3], gold={3, 99}, k=3) == 1.0
    assert recall_at_k([1, 2, 3], gold={99}, k=3) == 0.0


def test_recall_respects_k() -> None:
    assert recall_at_k([1, 2, 3], gold={3}, k=2) == 0.0
    assert recall_at_k([1, 2, 3], gold={3}, k=3) == 1.0


def test_mrr_uses_first_correct_position() -> None:
    assert mrr_at_k([9, 8, 3], gold={3}, k=10) == pytest.approx(1 / 3)
    assert mrr_at_k([3, 8, 9], gold={3}, k=10) == 1.0


def test_mrr_is_zero_when_nothing_hits() -> None:
    assert mrr_at_k([1, 2], gold={99}, k=10) == 0.0


def test_empty_retrieval_scores_zero() -> None:
    assert recall_at_k([], gold={1}, k=10) == 0.0
    assert mrr_at_k([], gold={1}, k=10) == 0.0


def test_empty_gold_is_a_programming_error() -> None:
    """eval_retrieval.gold_block_ids 有 CHECK 约束，不该出现空 gold。"""
    with pytest.raises(ValueError, match="gold"):
        recall_at_k([1], gold=set(), k=10)
