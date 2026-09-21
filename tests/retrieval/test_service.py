"""端到端检索：五个阶段串起来，统计齐全，时点正确。"""

from __future__ import annotations

from datetime import datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.rerank import FailingReranker, MockReranker, Reranker
from ragdemo.retrieval.rewrite import expand_synonyms
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest


def _service(conn: psycopg.Connection, reranker: Reranker | None = None) -> RetrievalService:
    return RetrievalService(conn, MockEmbedder(), reranker or MockReranker())


@pytest.mark.db
def test_end_to_end_returns_evidence(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(RetrievalRequest(query="云端训练芯片 收入", as_of=as_of_2024))
    assert result.blocks
    assert all(b.block_id > 0 for b in result.blocks)
    assert all(b.known_at <= as_of_2024 for b in result.blocks)


@pytest.mark.db
def test_stats_are_populated_for_every_stage(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """stats 是排查「为什么没找到」的唯一依据，每次查询都要记（06 §8）。"""
    result = _service(corpus).search(RetrievalRequest(query="收入", as_of=as_of_2024))
    s = result.stats
    assert s.bm25_hits >= 0 and s.vec_hits > 0
    assert s.after_fusion > 0
    assert s.ms_total > 0
    assert s.after_rerank == len(result.blocks) or s.after_rerank >= len(result.blocks)


@pytest.mark.db
def test_future_document_never_appears(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(RetrievalRequest(query="采购合同 8.5 亿元", as_of=as_of_2024))
    assert all(b.doc_id != 2 for b in result.blocks)


@pytest.mark.db
def test_same_query_same_as_of_is_reproducible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """可复现是引擎的核心承诺（00-overview §6 第 1 条）。"""
    svc = _service(corpus)
    req = RetrievalRequest(query="云端训练芯片", as_of=as_of_2024)
    first = [b.block_id for b in svc.search(req).blocks]
    second = [b.block_id for b in svc.search(req).blocks]
    assert first == second


@pytest.mark.db
def test_rerank_failure_degrades_but_still_returns(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(query="收入", as_of=as_of_2024)
    )
    assert result.blocks
    assert result.stats.degraded is True
    assert all(b.reranked is False for b in result.blocks)


@pytest.mark.db
def test_rerank_can_be_disabled(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus, FailingReranker()).search(
        RetrievalRequest(
            query="收入", as_of=as_of_2024, config=RetrievalConfig(rerank_enabled=False)
        )
    )
    assert result.blocks
    assert result.stats.ms_rerank == 0.0


@pytest.mark.db
def test_top_k_is_respected(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    result = _service(corpus).search(
        RetrievalRequest(query="收入", as_of=as_of_2024, config=RetrievalConfig(top_k=1))
    )
    assert len(result.blocks) <= 1


def test_synonym_expansion_adds_variants() -> None:
    table = {"算力": ["计算力", "compute"]}
    assert set(expand_synonyms("算力需求", table)) == {"算力需求", "计算力需求", "compute需求"}


def test_synonym_expansion_is_noop_without_matches() -> None:
    assert expand_synonyms("寒武纪业绩", {"算力": ["计算力"]}) == ["寒武纪业绩"]


@pytest.mark.db
def test_rewrite_is_off_by_default(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """默认关闭是有意的：改写增加延迟与成本，收益必须由评测证明（06 §7）。"""
    assert RetrievalConfig().rewrite_enabled is False
    result = _service(corpus).search(RetrievalRequest(query="算力", as_of=as_of_2024))
    assert result.stats.ms_total > 0
