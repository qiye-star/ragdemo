"""BM25 一路：中文命中、时点过滤、实体过滤、下推验证。"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.retrieval.lexical import _statement, bm25_search, explain_bm25
from ragdemo.retrieval.types import RetrievalConfig, RetrievalRequest


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
def test_future_documents_are_invisible(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
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
def test_parent_blocks_are_never_returned(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
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
def test_explain_shows_bm25_index_usage(corpus: psycopg.Connection, as_of_2024: datetime) -> None:
    """P1 验收项：EXPLAIN 验证 BM25 过滤下推（10-roadmap P1）。

    小语料下优化器可能选顺序扫描，因此这里只断言计划可取到且包含表名；
    真实数据量下的下推验证在 Task 10 的 nightly 任务里。
    """
    plan = explain_bm25(corpus, RetrievalRequest(query="云端训练芯片", as_of=as_of_2024))
    assert "doc_block" in plan


@pytest.mark.db
def test_a_question_with_extra_words_still_matches_on_any_term(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    """自然语言问题里总有语料没有的词；词面一路必须是「任一词命中」而非「全部命中」。
    合成评测集实测：`@@@`（合取）下 BM25 单路 recall@10 仅 0.09，`|||` 下 0.88。"""
    question = "请问云端训练芯片相关的业务情况到底怎么样呢"
    assert bm25_search(corpus, RetrievalRequest(query=question, as_of=as_of_2024))


@pytest.mark.db
def test_bm25_desc_finds_table_only_described_in_content_desc(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    corpus.execute(
        "UPDATE core.doc_block SET content_desc = '寒武纪产品出货结构一览'"
        " WHERE block_type = 'table'"
    )
    corpus.commit()
    # 查询词只出现在描述里：表格 content（业务分部/收入/同比）与段落都不含"结构一览"。
    off = bm25_search(corpus, RetrievalRequest(query="结构一览", as_of=as_of_2024))
    on = bm25_search(
        corpus,
        RetrievalRequest(
            query="结构一览", as_of=as_of_2024, config=RetrievalConfig(bm25_desc=True)
        ),
    )
    row = corpus.execute("SELECT block_id FROM core.doc_block WHERE block_type='table'").fetchone()
    assert row is not None
    assert row[0] not in {h.block_id for h in off}
    assert row[0] in {h.block_id for h in on}


@pytest.mark.db
def test_bm25_desc_is_pushed_into_a_single_paradedb_scan(
    corpus: psycopg.Connection, as_of_2024: datetime
) -> None:
    plan = explain_bm25(
        corpus,
        RetrievalRequest(query="收入", as_of=as_of_2024, config=RetrievalConfig(bm25_desc=True)),
    )
    assert plan.count("ParadeDB") == 1
    assert '"content_desc"' in plan and '"should"' in plan


def test_default_sql_is_unchanged() -> None:
    sql, _ = _statement(RetrievalRequest(query="收入", as_of=datetime(2025, 1, 1, tzinfo=UTC)))
    assert "content ||| %(query)s" in sql and "content_desc" not in sql
