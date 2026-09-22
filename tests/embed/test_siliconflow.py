"""SiliconFlowEmbedder：协议映射（乱序 index、维度校验、批量切分），
契约测试证明它与 MockEmbedder 行为等价。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ragdemo.adapters.errors import UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.embed.base import EMBEDDING_DIM
from ragdemo.embed.siliconflow import SiliconFlowEmbedder
from tests.contracts.embedder_contract import EmbedderContract


def _ok_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    data = [
        {"index": i, "embedding": [float(i + 1)] * EMBEDDING_DIM} for i in range(len(body["input"]))
    ]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 1}})


def _client(handler: httpx.MockTransport | None = None, **kw: object) -> HttpClient:
    return HttpClient(
        provider="siliconflow",
        base_url="https://example.test",
        policy=RetryPolicy(max_attempts=2, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=handler or httpx.MockTransport(_ok_handler),
        **kw,  # type: ignore[arg-type]
    )


class TestSiliconFlowEmbedder(EmbedderContract):
    def make_embedder(self) -> SiliconFlowEmbedder:
        return SiliconFlowEmbedder(_client(), model="test-embed-model")


def test_model_name_is_whatever_was_configured() -> None:
    embedder = SiliconFlowEmbedder(_client(), model="BAAI/bge-m3")
    assert embedder.model == "BAAI/bge-m3"


def _one_hot(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def test_out_of_order_index_is_reordered_to_match_input_position() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"])
        # 故意倒序返回——协议约定看 index 字段，不是数组下标本身。用
        # one-hot 向量而不是常数向量：常数向量做 L2 归一化后所有分量都变成
        # 同一个值，会把"顺序对不对"这件事本身也一起抹平，测不出乱序 bug。
        data = [{"index": i, "embedding": _one_hot(i)} for i in reversed(range(n))]
        return httpx.Response(200, json={"data": data})

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m")
    vectors = embedder.embed(["a", "b", "c"])
    assert vectors[0][0] == 1.0  # 输入 "a"（index 0）对应的向量在位置 0
    assert vectors[1][1] == 1.0
    assert vectors[2][2] == 1.0


def test_wrong_dimension_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(ValueError, match="1024"):
        embedder.embed(["only one"])


def test_missing_index_field_raises_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * EMBEDDING_DIM}]})

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        embedder.embed(["x"])


def test_response_count_mismatch_raises_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.0] * EMBEDDING_DIM}]}
        )

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        embedder.embed(["x", "y"])  # 2 个输入，响应只有 1 条


def test_batches_split_at_batch_size() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(len(body["input"]))
        data = [{"index": i, "embedding": [0.1] * EMBEDDING_DIM} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data})

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m", batch_size=2)
    vectors = embedder.embed(["a", "b", "c", "d", "e"])
    assert calls == [2, 2, 1]
    assert len(vectors) == 5


def test_rate_limit_propagates_as_upstream_unavailable() -> None:
    """post_json 已经处理了重试/分类——这里只验证 SiliconFlowEmbedder 没有
    在中间吞掉或者掩盖它，不重新实现一遍分类逻辑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    embedder = SiliconFlowEmbedder(_client(httpx.MockTransport(handler)), model="m")
    with pytest.raises(UpstreamUnavailable):
        embedder.embed(["x"])
