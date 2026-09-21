"""检索请求与配置的不变量。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


def test_defaults_match_the_spec() -> None:
    cfg = RetrievalConfig()
    assert (cfg.candidate_k, cfg.fusion_k, cfg.top_k) == (50, 50, 10)
    assert (cfg.w_bm25, cfg.w_vec, cfg.rrf_k) == (0.6, 0.4, 60)
    assert cfg.rewrite_enabled is False, "查询改写默认关闭，开启需评测证明"
    assert cfg.rerank_enabled is True


def test_as_of_is_required_and_has_no_default() -> None:
    with pytest.raises(TypeError):
        RetrievalRequest(query="算力")  # type: ignore[call-arg]


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        RetrievalRequest(query="算力", as_of=datetime(2024, 12, 31))


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query"):
        RetrievalRequest(query="   ", as_of=AS_OF)


def test_top_k_larger_than_fusion_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="top_k"):
        RetrievalConfig(fusion_k=10, top_k=50).validate()


def test_weights_must_be_positive() -> None:
    with pytest.raises(ValueError, match="权重"):
        RetrievalConfig(w_bm25=0.0, w_vec=0.0).validate()
