"""任何 Reranker 实现都必须通过的契约。

用法同 `embedder_contract.py`：子类化 `RerankerContract`，实现
`make_reranker()`。真实适配器（SiliconFlowReranker）与 `MockReranker`
走同一套契约——`retrieval/rerank.py::rerank_or_degrade` 只依赖这个契约，
不关心具体实现，两者能互换正是「建而不接线」（裁决 5）成立的前提。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class _RerankerLike(Protocol):
    model: str

    def rerank(self, query: str, docs: Sequence[str], top_k: int) -> list[tuple[int, float]]: ...


class RerankerContract:
    """契约测试基类。不要在这里加 pytest fixture，子类可能有自己的。"""

    def make_reranker(self) -> _RerankerLike:
        raise NotImplementedError

    def test_model_is_a_nonempty_string(self) -> None:
        model = self.make_reranker().model
        assert isinstance(model, str) and model

    def test_result_length_is_at_most_top_k(self) -> None:
        docs = [f"文档{i}" for i in range(5)]
        ranked = self.make_reranker().rerank("查询", docs, top_k=2)
        assert len(ranked) <= 2

    def test_scores_are_non_increasing(self) -> None:
        docs = ["苹果派", "香蕉船", "樱桃蛋糕", "枣泥饼"]
        ranked = self.make_reranker().rerank("苹果", docs, top_k=4)
        scores = [score for _, score in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_indices_are_unique_and_within_range(self) -> None:
        docs = ["a", "b", "c"]
        ranked = self.make_reranker().rerank("q", docs, top_k=3)
        indices = [i for i, _ in ranked]
        assert len(indices) == len(set(indices))
        assert all(0 <= i < len(docs) for i in indices)

    def test_empty_docs_returns_empty(self) -> None:
        assert self.make_reranker().rerank("q", [], top_k=5) == []
