"""SiliconFlowReranker：协议映射（未排序响应、越界 index、缺字段），
契约测试证明它与 MockReranker 行为等价。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.retrieval.rerank_siliconflow import SiliconFlowReranker
from tests.contracts.reranker_contract import RerankerContract


def _score_by_length_handler(request: httpx.Request) -> httpx.Response:
    """一个确定性的假重排：分数就是文档长度，天然可排序、可复现。"""
    body = json.loads(request.content)
    docs = body["documents"]
    results = [{"index": i, "relevance_score": float(len(d))} for i, d in enumerate(docs)]
    return httpx.Response(200, json={"results": results})


def _client(handler: httpx.MockTransport | None = None, **kw: object) -> HttpClient:
    return HttpClient(
        provider="siliconflow",
        base_url="https://example.test",
        policy=RetryPolicy(max_attempts=2, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=handler or httpx.MockTransport(_score_by_length_handler),
        **kw,  # type: ignore[arg-type]
    )


class TestSiliconFlowReranker(RerankerContract):
    def make_reranker(self) -> SiliconFlowReranker:
        return SiliconFlowReranker(_client(), model="test-rerank-model")


def test_model_name_is_whatever_was_configured() -> None:
    reranker = SiliconFlowReranker(_client(), model="BAAI/bge-reranker-v2-m3")
    assert reranker.model == "BAAI/bge-reranker-v2-m3"


def test_empty_docs_never_makes_a_network_call() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    assert reranker.rerank("q", [], top_k=5) == []
    assert calls["n"] == 0


def test_unsorted_upstream_response_is_sorted_descending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # 故意乱序、且分数不递减——协议契约要求调用方自己排序，不假设上游
        # 已经排好。
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.1},
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
        )

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    ranked = reranker.rerank("q", ["a", "b", "c"], top_k=3)
    assert ranked == [(0, 0.9), (1, 0.5), (2, 0.1)]


def test_result_truncated_to_top_k_after_sorting() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.5},
                ]
            },
        )

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    ranked = reranker.rerank("q", ["a", "b", "c"], top_k=1)
    assert ranked == [(1, 0.9)]


def test_out_of_range_index_raises_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 5, "relevance_score": 0.9}]})

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        reranker.rerank("q", ["a", "b"], top_k=2)


def test_missing_results_key_raises_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": []})

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        reranker.rerank("q", ["a"], top_k=1)


def test_missing_relevance_score_field_raises_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0}]})

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        reranker.rerank("q", ["a"], top_k=1)


def test_request_body_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="my-model")
    reranker.rerank("我的查询", ["d1", "d2"], top_k=7)

    body = json.loads(seen[0].content)
    assert body == {
        "model": "my-model",
        "query": "我的查询",
        "documents": ["d1", "d2"],
        "top_n": 7,
        "return_documents": False,
    }


def test_rerank_failure_degrades_via_rerank_or_degrade_not_swallowed_here() -> None:
    """这个适配器不该自己 try/except——降级判断只属于 rerank_or_degrade。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    reranker = SiliconFlowReranker(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        reranker.rerank("q", ["a"], top_k=1)
