"""重排与降级：失败时回退 RRF 顺序，不让查询失败。"""
from __future__ import annotations

import pytest
from ragdemo.retrieval.fusion import FusedHit

from ragdemo.retrieval.rerank import (
    FailingReranker,
    MockReranker,
    rerank_or_degrade,
)

FUSED = [
    FusedHit(block_id=1, score=0.03, bm25_rank=1, vec_rank=2),
    FusedHit(block_id=2, score=0.02, bm25_rank=2, vec_rank=None),
    FusedHit(block_id=3, score=0.01, bm25_rank=None, vec_rank=1),
]
CONTENTS = {
    1: "云端训练芯片出货量提升",
    2: "研发费用同比增长",
    3: "云端训练芯片采购合同",
}


def test_reranker_reorders_by_relevance() -> None:
    out = rerank_or_degrade(MockReranker(), "采购合同", FUSED, CONTENTS, top_k=3)
    assert out.order[0] == 3
    assert out.degraded is False


def test_top_k_is_respected() -> None:
    out = rerank_or_degrade(MockReranker(), "云端训练芯片", FUSED, CONTENTS, top_k=2)
    assert len(out.order) == 2


def test_failure_degrades_to_rrf_order() -> None:
    """研究场景下「慢」比「没有结果」更不可接受（06 §5）。"""
    out = rerank_or_degrade(FailingReranker(), "任意查询", FUSED, CONTENTS, top_k=3)
    assert out.order == [1, 2, 3]
    assert out.degraded is True


def test_degraded_scores_fall_back_to_rrf_scores() -> None:
    out = rerank_or_degrade(FailingReranker(), "任意", FUSED, CONTENTS, top_k=3)
    assert out.scores[1] == pytest.approx(0.03)


def test_empty_input_short_circuits_without_calling_the_model() -> None:
    out = rerank_or_degrade(FailingReranker(), "任意", [], {}, top_k=10)
    assert out.order == []
    assert out.degraded is False


def test_missing_content_is_treated_as_empty_not_a_crash() -> None:
    out = rerank_or_degrade(MockReranker(), "云端", FUSED, {1: "云端训练芯片"}, top_k=3)
    assert len(out.order) == 3
