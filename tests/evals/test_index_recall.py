"""索引召回率：索引路径相对精确扫描的召回（06 §3.6，阈值 ≥ 0.95）。"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.index_recall import exact_vector_search, index_recall
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalRequest

AS_OF = datetime(2024, 12, 31, tzinfo=UTC)


@pytest.mark.db
def test_exact_scan_returns_results(corpus: psycopg.Connection) -> None:
    qvec = MockEmbedder().embed(["云端训练芯片"])[0]
    ids = exact_vector_search(
        corpus, RetrievalRequest(query="云端训练芯片", as_of=AS_OF), qvec, limit=10
    )
    assert ids


@pytest.mark.db
def test_exact_scan_respects_the_time_point(corpus: psycopg.Connection) -> None:
    qvec = MockEmbedder().embed(["采购合同"])[0]
    early = exact_vector_search(
        corpus, RetrievalRequest(query="采购合同", as_of=AS_OF), qvec, limit=10
    )
    late = exact_vector_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
        qvec,
        limit=10,
    )
    assert len(late) > len(early)


@pytest.mark.db
def test_index_recall_is_one_on_a_tiny_corpus(corpus: psycopg.Connection) -> None:
    """小语料下索引与精确扫描应完全一致。差异出现在大语料上。"""
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    cases = [RetrievalRequest(query=q, as_of=AS_OF) for q in ("云端训练芯片", "研发费用", "收入")]
    assert index_recall(corpus, service, MockEmbedder(), cases) == pytest.approx(1.0)


@pytest.mark.db
def test_index_recall_raises_on_empty_cases(corpus: psycopg.Connection) -> None:
    """空用例算出满分会让 nightly 门禁形同虚设（同 evals/runner.py 的既有约定）。"""
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    with pytest.raises(ValueError, match="用例为空"):
        index_recall(corpus, service, MockEmbedder(), [])
