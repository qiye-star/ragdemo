"""SiliconFlow 客户端构造：config/providers.yaml 的 siliconflow 段接进
HttpClient，base_url 由调用方覆盖（回环反向代理），不是 yaml 里的生产域名。
"""

from __future__ import annotations

import httpx

from ragdemo.adapters.siliconflow import (
    EMBEDDINGS_ENDPOINT,
    PROVIDER,
    RERANK_ENDPOINT,
    client_from_config,
)


def test_client_from_config_uses_the_passed_base_url_not_the_yaml_one() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = client_from_config("http://127.0.0.1:8082", transport=httpx.MockTransport(handler))
    client.post_json(EMBEDDINGS_ENDPOINT, {"model": "bge-m3", "input": ["x"]})

    assert client.provider == PROVIDER
    assert str(seen[0].url).startswith("http://127.0.0.1:8082")


def test_client_from_config_reads_retry_and_quota_from_providers_yaml() -> None:
    client = client_from_config("http://127.0.0.1:8082")
    assert client.daily_quota == 20000
    assert client.policy.max_attempts == 4


def test_endpoints_match_siliconflow_api_paths() -> None:
    assert EMBEDDINGS_ENDPOINT == "/v1/embeddings"
    assert RERANK_ENDPOINT == "/v1/rerank"
