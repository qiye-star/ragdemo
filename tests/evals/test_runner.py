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
