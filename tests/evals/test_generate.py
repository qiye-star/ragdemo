"""合成检索评测用例：过滤、合规、同文并列 gold、可区分于人工用例（阶段 5）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.evals.generate import (
    generate_cases,
    insert_cases,
    lexical_overlap,
    measured_difficulty,
)


def _http(reply: Any) -> tuple[HttpClient, list[str]]:  # noqa: ANN401
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        text = reply(len(prompts)) if callable(reply) else reply
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=60000),
        transport=httpx.MockTransport(handler),
    )
    return client, prompts


def test_lexical_overlap_and_difficulty_labels() -> None:
    passage = "报告期内云端训练芯片出货量提升，智能计算集群系统业务收入增长"
    assert lexical_overlap("云端训练芯片出货量提升了吗？", passage) > 0.75
    assert lexical_overlap("这家公司有什么新产品线？", passage) < 0.3
    assert measured_difficulty(0.9) == "easy"
    assert measured_difficulty(0.6) == "medium"
    assert measured_difficulty(0.2) == "hard"


@pytest.mark.db
def test_generates_cases_with_provenance_and_marks_them_as_synthetic(
    corpus: psycopg.Connection,
) -> None:
    http, prompts = _http(lambda i: f"寒武纪云端训练芯片相关的第{i}个问题是什么？")
    cases, _report = generate_cases(corpus, http, model="deepseek-chat", n=3, min_body_chars=10)
    assert 1 <= len(cases) <= 3
    assert all(c.question.endswith("？") for c in cases)
    assert all(c.gold_block_ids for c in cases)
    assert all("合成用例：deepseek-chat" in c.notes and "词面重合度" in c.notes for c in cases)
    assert all(c.difficulty in ("easy", "medium", "hard") for c in cases)
    assert len(prompts) >= len(cases)
    n = insert_cases(corpus, cases, as_of=datetime(2026, 1, 1, tzinfo=UTC), author="auto:m:v1")
    row = corpus.execute(
        "SELECT count(*), count(*) FILTER (WHERE author LIKE 'auto:%') FROM evals.eval_retrieval"
    ).fetchone()
    assert row == (n, n)


@pytest.mark.db
def test_questions_with_forbidden_wording_or_bad_shape_are_dropped(
    corpus: psycopg.Connection,
) -> None:
    replies = iter(["该不该买入寒武纪的股票？", "没有问号的句子", "寒武纪的研发费用是多少万元？"])
    http, _ = _http(lambda _i: next(replies, "寒武纪的研发费用是多少万元？"))
    cases, report = generate_cases(corpus, http, model="m", n=1, min_body_chars=10)
    assert [c.question for c in cases] == ["寒武纪的研发费用是多少万元？"], report.skipped
    assert (report.skipped or {}).get("bad_question", 0) == 2


@pytest.mark.db
def test_verbatim_and_duplicate_questions_are_skipped(corpus: psycopg.Connection) -> None:
    # 同一个问题反复出现：只保留一条
    http, _ = _http("寒武纪云端训练芯片的收入是多少？")
    cases, report = generate_cases(corpus, http, model="m", n=5, min_body_chars=10)
    assert len({c.question for c in cases}) == len(cases) == 1
    assert (report.skipped or {}).get("dup_question", 0) >= 1


@pytest.mark.db
def test_a_model_failure_skips_the_candidate_instead_of_aborting(
    corpus: psycopg.Connection,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    http = HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=60000),
        transport=httpx.MockTransport(handler),
    )
    cases, report = generate_cases(corpus, http, model="m", n=3, min_body_chars=10)
    assert cases == [] and any(k.startswith("llm_error") for k in (report.skipped or {}))


@pytest.mark.db
def test_identical_text_in_several_blocks_makes_all_of_them_gold(
    corpus: psycopg.Connection,
) -> None:
    """同一句固定表述在多份文档里重复出现时，只标一个 gold 会把"找对了另一份"判成错。"""
    text = "公司严格遵守相关法律法规，报告期内不存在应披露而未披露的重大事项，特此说明如下。" * 2
    for doc_id, ordinal in ((1, 90), (2, 90)):
        corpus.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, content,"
            " char_len, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, source,"
            " ingest_run_id) SELECT d.doc_id, 'paragraph', '第一节', %s, %s, %s, true, d.entity_id,"
            " d.doc_type, d.publish_at, '2024-07-01', d.known_at, 'mock', 'r1' FROM core.document d"
            " WHERE d.doc_id = %s",
            (ordinal, "第一节\n" + text, len(text) + 4, doc_id),
        )
    corpus.commit()
    http, _ = _http("报告期内是否存在应披露而未披露的重大事项？")
    cases, _ = generate_cases(corpus, http, model="m", n=50, min_body_chars=10)
    multi = [c for c in cases if len(c.gold_block_ids) > 1]
    assert multi, "同一实体两个文档里同一段话应当都被列为 gold"


def test_questions_with_vague_references_are_rejected() -> None:
    from ragdemo.evals.generate import _clean_question

    assert _clean_question("根据这份报告，该公司的营业收入是多少？") is None
    assert _clean_question("海光信息 2025 年半年度报告里的营业收入是多少？") is not None
