"""适配器错误 → 降级决策（ADR-0012 "什么时候切到下一层"）。

报文都是 2026-09-28 实测原文（Tushare MCP / REST 同一账号）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ragdemo.adapters.errors import (
    AdapterError,
    ProviderRefused,
    QuotaExceeded,
    RateLimited,
    UpstreamUnavailable,
)
from ragdemo.adapters.mcp_gateway import McpProtocolError, McpToolFailed
from ragdemo.adapters.wind import WindResponseRejected
from ragdemo.sources.failures import FailureKind, classify

NO_PERMISSION = (
    "tushare-mcp 工具 income 业务失败：tushare API 错误 [40203]: 抱歉，您没有接口(income)"
    "访问权限，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108。"
)
HOURLY = (
    "tushare-mcp 工具 stock_basic 业务失败：tushare API 错误 [40203]: 抱歉，您访问接口"
    "(stock_basic)频率超限(1次/小时)，具体频次详情：https://tushare.pro/document/1?doc_id=108。"
)
PER_MINUTE = "抱歉，您访问接口(trade_cal)频率超限(1次/分钟)，具体频次详情：https://tushare.pro/"


def test_no_permission_blocks_the_layer_for_a_day() -> None:
    failure = classify(McpToolFailed(NO_PERMISSION))
    assert failure.kind is FailureKind.PERMISSION
    assert failure.cooldown == timedelta(hours=24)


@pytest.mark.parametrize(
    ("message", "window"),
    [(HOURLY, timedelta(hours=1)), (PER_MINUTE, timedelta(minutes=1))],
)
def test_rate_limit_cooldown_follows_the_window_in_the_message(
    message: str, window: timedelta
) -> None:
    for exc in (McpToolFailed(message), ProviderRefused(message)):
        failure = classify(exc)
        assert failure.kind is FailureKind.RATE_LIMITED
        assert failure.cooldown == window


def test_http_429_uses_retry_after_then_a_default() -> None:
    assert classify(RateLimited("上游限速", retry_after_s=30)).cooldown == timedelta(seconds=30)
    assert classify(RateLimited("上游限速")).cooldown == timedelta(minutes=5)


def test_quota_exhausted_is_rate_limited_for_hours() -> None:
    failure = classify(QuotaExceeded("wind 已达每日配额 1000"))
    assert failure.kind is FailureKind.RATE_LIMITED
    assert failure.cooldown == timedelta(hours=6)


@pytest.mark.parametrize(
    "exc",
    [
        UpstreamUnavailable("tushare-mcp /mcp/ 重试 3 次后仍未拿到响应"),
        McpProtocolError("响应既没有 SSE data 行也不是 JSON"),
        McpToolFailed("wind-mcp 工具 get_stock_kline 业务失败：服务暂不可用"),
    ],
)
def test_transport_and_unknown_tool_failures_are_unavailable(exc: AdapterError) -> None:
    failure = classify(exc)
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.cooldown == timedelta(minutes=5)


def test_http_auth_status_is_permission() -> None:
    failure = classify(UpstreamUnavailable("ifind-mcp /x 返回 401（不重试）"))
    assert failure.kind is FailureKind.PERMISSION


def test_rejected_response_falls_through_without_cooldown() -> None:
    failure = classify(WindResponseRejected("科目列数不对"))
    assert failure.kind is FailureKind.REJECTED
    assert failure.cooldown == timedelta(0)


def test_message_is_truncated_for_storage() -> None:
    assert len(classify(UpstreamUnavailable("x" * 5000)).message) <= 500
