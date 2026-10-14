"""重排与降级：失败时回退 RRF 顺序，不让查询失败。"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import pytest

from ragdemo.retrieval.fusion import FusedHit
from ragdemo.retrieval.rerank import (
    FailingReranker,
    MockReranker,
    rerank_or_degrade,
)
from ragdemo.retrieval.types import RetrievalConfig

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


class _SlowReranker:
    """sleep 后再返回，模拟重排接口卡住。"""

    model = "slow"

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]:
        time.sleep(self.delay_s)
        return MockReranker().rerank(query, docs, top_k)


def test_slow_reranker_times_out_and_falls_back_to_fused_order() -> None:
    out = rerank_or_degrade(
        _SlowReranker(1.0), "采购合同", FUSED, CONTENTS, top_k=3, timeout_s=0.05
    )
    assert out.order == [1, 2, 3]
    assert out.degraded is True
    assert out.degraded_reason == "rerank_timeout"
    assert out.scores[1] == pytest.approx(0.03)


def test_fast_reranker_is_unaffected_by_timeout() -> None:
    out = rerank_or_degrade(MockReranker(), "采购合同", FUSED, CONTENTS, top_k=3, timeout_s=5.0)
    assert out.order[0] == 3
    assert out.degraded is False
    assert out.degraded_reason is None


def test_default_no_timeout_waits_for_slow_reranker() -> None:
    out = rerank_or_degrade(_SlowReranker(0.2), "采购合同", FUSED, CONTENTS, top_k=3)
    assert out.order[0] == 3
    assert out.degraded is False


def test_failure_reason_is_rerank_error_with_or_without_timeout() -> None:
    assert (
        rerank_or_degrade(FailingReranker(), "q", FUSED, CONTENTS, top_k=3).degraded_reason
        == "rerank_error"
    )
    assert (
        rerank_or_degrade(
            FailingReranker(), "q", FUSED, CONTENTS, top_k=3, timeout_s=5.0
        ).degraded_reason
        == "rerank_error"
    )


def test_timeout_logs_structured_line_with_context(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ragdemo.retrieval.rerank"):
        rerank_or_degrade(
            _SlowReranker(1.0),
            "q",
            FUSED,
            CONTENTS,
            top_k=3,
            timeout_s=0.05,
            log_context={"run_id": "r1", "as_of": "2024-01-01T00:00:00+00:00", "entity_id": ["E1"]},
        )
    rec = next(r for r in caplog.records if r.getMessage() == "rerank_degraded")
    assert rec.degraded_reason == "rerank_timeout"  # type: ignore[attr-defined]
    assert rec.run_id == "r1"  # type: ignore[attr-defined]
    assert rec.entity_id == ["E1"]  # type: ignore[attr-defined]


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(rerank_timeout_s=0).validate()
    assert RetrievalConfig().rerank_timeout_s is None
