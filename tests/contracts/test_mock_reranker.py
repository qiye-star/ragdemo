from __future__ import annotations

from ragdemo.retrieval.rerank import MockReranker
from tests.contracts.reranker_contract import RerankerContract


class TestMockReranker(RerankerContract):
    def make_reranker(self) -> MockReranker:
        return MockReranker()
