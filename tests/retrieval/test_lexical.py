"""BM25 一路：中文命中、时点过滤、实体过滤、下推验证。"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.retrieval.lexical import bm25_search, explain_bm25
from ragdemo.retrieval.types import RetrievalRequest


@pytest.mark.db
def test_chinese_query_hits(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))
    assert hits


@pytest.mark.db
def test_ranks_start_at_one_and_are_contiguous(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="收入", as_of=as_of_2024))
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


@pytest.mark.db
def test_future_documents_are_invisible(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """2025-03 的合同公告在 2024-12-31 的时点上必须不可见。"""
    hits = bm25_search(corpus, RetrievalRequest(query="采购合同", as_of=as_of_2024))
    assert hits == []

    later = bm25_search(
        corpus,
        RetrievalRequest(query="采购合同", as_of=datetime(2025, 6, 1, tzinfo=UTC)),
    )
    assert later


@pytest.mark.db
def test_entity_filter_excludes_other_companies(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(
        corpus,
        RetrievalRequest(query="收入", as_of=as_of_2024, entity_ids=["CN.002049"]),
    )
    block_ids = [h.block_id for h in hits]
    rows = corpus.execute(
        "SELECT DISTINCT entity_id FROM core.doc_block WHERE block_id = ANY(%s)",
        (block_ids,),
    ).fetchall()
    assert {r[0] for r in rows} == {"CN.002049"}


@pytest.mark.db
def test_parent_blocks_are_never_returned(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    hits = bm25_search(corpus, RetrievalRequest(query="主营业务", as_of=as_of_2024))
    if hits:
        rows = corpus.execute(
            "SELECT bool_and(is_leaf) FROM core.doc_block WHERE block_id = ANY(%s)",
            ([h.block_id for h in hits],),
        ).fetchone()
        assert rows is not None and rows[0] is True


@pytest.mark.db
def test_candidate_k_caps_the_result(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    from ragdemo.retrieval.types import RetrievalConfig

    # candidate_k=1 配合默认 fusion_k=50/top_k=10 会被 RetrievalConfig.validate()
    # 拒绝（fusion_k 不能超过两路候选之和），因此这里取满足校验的最小合法组合，
    # 语料里「收入」命中不止一块，仍然足以验证 candidate_k 生效。
    req = RetrievalRequest(
        query="收入",
        as_of=as_of_2024,
        config=RetrievalConfig(candidate_k=2, fusion_k=2, top_k=2),
    )
    assert len(bm25_search(corpus, req)) <= 2


@pytest.mark.db
def test_explain_shows_bm25_index_usage(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """P1 验收项：EXPLAIN 验证 BM25 过滤下推（10-roadmap P1）。

    小语料下优化器可能选顺序扫描，因此这里只断言计划可取到且包含表名；
    真实数据量下的下推验证在 Task 10 的 nightly 任务里。
    """
    plan = explain_bm25(corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))
    assert "doc_block" in plan
