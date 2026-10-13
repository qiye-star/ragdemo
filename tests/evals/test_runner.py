"""评测框架：跑完写一行 eval_run，含 git_sha 与完整 config。"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from ragdemo.embed.mock import MockEmbedder
from ragdemo.evals.runner import run_retrieval_eval
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo.retrieval.types import RetrievalConfig


def _seed_cases(conn: psycopg.Connection) -> None:
    gold = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE content LIKE '%云端训练芯片%'"
        " AND is_leaf LIMIT 1"
    ).fetchone()
    assert gold is not None
    conn.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids,"
        " entity_filter, difficulty, author) "
        "VALUES ('寒武纪三季度云端训练芯片业务表现如何？', %s, %s, %s, 'easy', 'founder')",
        (datetime(2024, 12, 31, tzinfo=UTC), [int(gold[0])], ["CN.688256"]),
    )
    conn.commit()


@pytest.mark.db
def test_eval_computes_metrics(corpus: psycopg.Connection) -> None:
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="abc1234", config=RetrievalConfig())
    assert result.n_cases == 1
    assert 0.0 <= result.recall_at_10 <= 1.0


@pytest.mark.db
def test_eval_writes_a_run_row_with_sha_and_config(corpus: psycopg.Connection) -> None:
    """没有这张表，三个月后看到指标下降无法定位是哪个参数改的（08 §4.3）。"""
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    run_retrieval_eval(
        corpus, service, git_sha="abc1234", config=RetrievalConfig(w_bm25=0.7, w_vec=0.3)
    )
    row = corpus.execute(
        "SELECT suite, git_sha, config, metrics FROM evals.eval_run ORDER BY started_at DESC"
        " LIMIT 1"
    ).fetchone()
    assert row is not None
    suite, git_sha, config, metrics = row
    assert suite == "retrieval"
    assert git_sha == "abc1234"
    assert config["w_bm25"] == 0.7
    assert "recall_at_10" in metrics


@pytest.mark.db
def test_eval_reports_per_difficulty(corpus: psycopg.Connection) -> None:
    _seed_cases(corpus)
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="x", config=RetrievalConfig())
    assert "easy" in result.per_difficulty


@pytest.mark.db
def test_empty_eval_set_raises_rather_than_reporting_perfect_score(
    corpus: psycopg.Connection,
) -> None:
    """空评测集算出 1.0 会让 CI 门禁形同虚设。"""
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    with pytest.raises(ValueError, match="评测集为空"):
        run_retrieval_eval(corpus, service, git_sha="x", config=RetrievalConfig())


def test_percentile_uses_nearest_rank_without_interpolation() -> None:
    from ragdemo.evals.runner import _percentile

    values = [float(v) for v in range(1, 101)]  # 1..100
    assert _percentile(values, 0.95) == 95.0
    assert _percentile(values, 0.50) == 50.0
    assert _percentile([7.0], 0.95) == 7.0
    assert _percentile([], 0.95) == 0.0
    assert _percentile([3.0, 1.0, 2.0], 0.95) == 3.0  # 乱序输入也按排序后取


@pytest.mark.db
def test_author_prefix_restricts_the_run_to_human_or_auto_cases(
    corpus: psycopg.Connection,
) -> None:
    _seed_cases(corpus)  # author = 'founder'
    corpus.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, difficulty, author)"
        " SELECT '寒武纪云端训练芯片业务表现如何？', %s, ARRAY[block_id], 'easy', 'human:张三'"
        " FROM core.doc_block WHERE is_leaf LIMIT 1",
        (datetime(2024, 12, 31, tzinfo=UTC),),
    )
    corpus.commit()
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    cfg = RetrievalConfig()
    assert run_retrieval_eval(corpus, service, git_sha="x", config=cfg).n_cases == 2
    human = run_retrieval_eval(corpus, service, git_sha="x", config=cfg, author_prefix="human:")
    assert human.n_cases == 1
    with pytest.raises(ValueError):  # 没有 auto: 用例时不能静默给出满分
        run_retrieval_eval(corpus, service, git_sha="x", config=cfg, author_prefix="auto:")


@pytest.mark.db
def test_eval_reports_per_group_with_table_gold(corpus: psycopg.Connection) -> None:
    table = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    assert table is not None
    corpus.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, difficulty, author)"
        " VALUES ('寒武纪智能计算分部收入是多少？', %s, %s, 'easy', 'auto:test:v1')",
        (datetime(2024, 12, 31, tzinfo=UTC), [int(table[0])]),
    )
    corpus.commit()
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="abc1234", config=RetrievalConfig())
    assert set(result.per_group) == {"table/auto"}
    assert result.per_group["table/auto"]["n"] == 1
    assert "per_group" in result.as_metrics()


@pytest.mark.db
def test_eval_reports_per_engine_by_parse_source(corpus: psycopg.Connection) -> None:
    leaf = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE is_leaf AND block_type = 'paragraph'"
        " ORDER BY block_id LIMIT 1"
    ).fetchone()
    assert leaf is not None
    corpus.execute(
        "UPDATE core.doc_block SET parse_source = 'local' WHERE block_id = %s", (int(leaf[0]),)
    )
    corpus.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, difficulty, author)"
        " VALUES ('寒武纪的主营业务是什么？', %s, %s, 'easy', 'human:t')",
        (datetime(2024, 12, 31, tzinfo=UTC), [int(leaf[0])]),
    )
    corpus.commit()
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(
        corpus, service, git_sha="x", config=RetrievalConfig(), author_prefix="human:"
    )
    assert set(result.per_engine) == {"local/human"} and result.per_engine["local/human"]["n"] == 1
    assert "per_engine" in result.as_metrics()


@pytest.mark.db
def test_eval_persists_per_question_ranks(corpus: psycopg.Connection) -> None:
    table = corpus.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' LIMIT 1"
    ).fetchone()
    assert table is not None
    gold = int(table[0])
    corpus.execute(
        "INSERT INTO evals.eval_retrieval (question, as_of, gold_block_ids, difficulty, author)"
        " VALUES ('寒武纪智能计算分部收入是多少？', %s, %s, 'easy', 'auto:test:v1')",
        (datetime(2024, 12, 31, tzinfo=UTC), [gold]),
    )
    corpus.commit()
    service = RetrievalService(corpus, MockEmbedder(), MockReranker())
    result = run_retrieval_eval(corpus, service, git_sha="x", config=RetrievalConfig())
    assert len(result.per_question) == 1
    pq = result.per_question[0]
    assert pq["gold"] == [gold]
    for key in ("bm25_rank", "vec_rank", "fused_rank", "rerank_rank", "final_rank"):
        assert key in pq
    assert pq["final_rank"] is None or pq["final_rank"] >= 1
    assert result.as_metrics()["per_question"] == result.per_question
    stored = corpus.execute(
        "SELECT metrics->'per_question' FROM evals.eval_run ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert stored is not None and stored[0][0]["gold"] == [gold]
