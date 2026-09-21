"""HTTP 客户端：退避、Retry-After、重试耗尽后失败而非返回部分数据。"""

from __future__ import annotations

import logging

import httpx
import pytest

from ragdemo.adapters.errors import RateLimited, UpstreamUnavailable
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket


def _client(handler: httpx.MockTransport, **kw: object) -> HttpClient:
    return HttpClient(
        provider="test",
        base_url="https://example.test",
        policy=RetryPolicy(max_attempts=3, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=handler,
        **kw,  # type: ignore[arg-type]
    )


def test_successful_request_returns_raw_response() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    raw = _client(transport).get_json("/x", {"a": 1})
    assert raw.http_status == 200
    assert raw.payload == {"ok": True}
    assert raw.fetched_at.tzinfo is not None


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    raw = _client(httpx.MockTransport(handler)).get_json("/x", {})
    assert raw.payload == {"ok": True}
    assert calls["n"] == 3


def test_exhausted_retries_raise_instead_of_returning_partial() -> None:
    """部分数据入库比没数据更糟——下游会以为数据完整。"""
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(UpstreamUnavailable):
        _client(transport).get_json("/x", {})


def test_429_surfaces_retry_after() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(UpstreamUnavailable) as exc:
        _client(transport).get_json("/x", {})
    assert isinstance(exc.value.__cause__, RateLimited)
    assert exc.value.__cause__.retry_after_s == 7.0


def test_4xx_other_than_429_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(UpstreamUnavailable):
        _client(httpx.MockTransport(handler)).get_json("/x", {})
    assert calls["n"] == 1, "404 不该重试"


def test_token_bucket_limits_rate() -> None:
    bucket = TokenBucket(rate_per_minute=60)
    assert bucket.acquire() == 0.0
    assert bucket.acquire() > 0.0


def test_secret_query_param_never_reaches_logs(caplog: pytest.LogCaptureFixture) -> None:
    """httpx 默认在 INFO 级别打印完整请求行（含查询串）。CLAUDE.md §3 要求密钥
    不得出现在日志中——importing ragdemo.adapters.http 必须已经把 httpx 自己的
    logger 降到 WARNING，让这条 INFO 日志根本不产生，而不是依赖调用方脱敏。

    这里特意不对 "httpx" logger 调用 caplog.set_level：那样会把它重新调回
    INFO，等于绕过被测的修复。只在根 logger 上设置 INFO 阈值，验证即便
    根/处理器愿意接收 INFO，httpx 自己的 logger 仍然因为被设成 WARNING
    而不产生这条日志。
    """
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    with caplog.at_level(logging.INFO):
        _client(transport).get_json("/x", {"token": "sk-SECRET-VALUE"})

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    for record in caplog.records:
        assert "sk-SECRET-VALUE" not in record.getMessage()


def test_200_with_invalid_json_raises_upstream_unavailable() -> None:
    """200 响应但体不是有效 JSON 时，应抛 UpstreamUnavailable 而非原始 JSONDecodeError。"""
    import json

    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(UpstreamUnavailable) as exc:
        _client(transport).get_json("/x", {})
    assert isinstance(exc.value.__cause__, json.JSONDecodeError)
