"""把适配器错误归成四类，决定这一层冷却多久（ADR-0012 "什么时候切到下一层"）。

只归类 `AdapterError`：别的异常（KeyError、TypeError……）是我们自己的 bug，
当成"供应商不可用"去降级会把 bug 藏起来，由调用方原样抛出。

报文判断基于 2026-09-28 实测原文，例如 Tushare 的
`抱歉，您没有接口(income)访问权限` 与 `抱歉，您访问接口(stock_basic)频率超限(1次/小时)`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from ragdemo.adapters.errors import (
    AdapterError,
    ProviderRefused,
    QuotaExceeded,
    RateLimited,
    UpstreamUnavailable,
)
from ragdemo.adapters.mcp_gateway import McpProtocolError, McpToolFailed

PERMISSION_COOLDOWN = timedelta(hours=24)  # 改权限要人工，一天查一次足够
QUOTA_COOLDOWN = timedelta(hours=6)
DEFAULT_COOLDOWN = timedelta(minutes=5)
MAX_MESSAGE = 500

_PERMISSION_MARKERS = ("访问权限", "没有接口", "无权限", "未授权", "401", "403")
_RATE_MARKERS = ("频率超限", "频繁", "限流", "超限", "429")
_WINDOW = re.compile(r"(\d+)\s*次\s*/\s*(秒|分钟|小时|天)")
_UNIT = {
    "秒": timedelta(seconds=1),
    "分钟": timedelta(minutes=1),
    "小时": timedelta(hours=1),
    "天": timedelta(days=1),
}


class FailureKind(StrEnum):
    PERMISSION = "permission"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    message: str
    cooldown: timedelta


def classify(exc: AdapterError) -> Failure:
    message = str(exc)[:MAX_MESSAGE]

    if isinstance(exc, RateLimited):
        wait = timedelta(seconds=exc.retry_after_s) if exc.retry_after_s else DEFAULT_COOLDOWN
        return Failure(FailureKind.RATE_LIMITED, message, wait)
    if isinstance(exc, QuotaExceeded):
        return Failure(FailureKind.RATE_LIMITED, message, QUOTA_COOLDOWN)

    if isinstance(exc, McpToolFailed | ProviderRefused | UpstreamUnavailable):
        if any(m in message for m in _PERMISSION_MARKERS):
            return Failure(FailureKind.PERMISSION, message, PERMISSION_COOLDOWN)
        if any(m in message for m in _RATE_MARKERS):
            return Failure(FailureKind.RATE_LIMITED, message, _window(message))
        return Failure(FailureKind.UNAVAILABLE, message, DEFAULT_COOLDOWN)
    if isinstance(exc, McpProtocolError):
        return Failure(FailureKind.UNAVAILABLE, message, DEFAULT_COOLDOWN)

    # 其余 AdapterError 是响应校验不过（如 WindResponseRejected）：这一层这一次的数据
    # 不可信，但不说明它坏了——下次照常先问它。
    return Failure(FailureKind.REJECTED, message, timedelta(0))


def _window(message: str) -> timedelta:
    """把"频率超限(1次/小时)"换成 1 小时：限频窗口过去之前再问一定还是被拒。"""
    match = _WINDOW.search(message)
    if match is None:
        return DEFAULT_COOLDOWN
    return _UNIT[match.group(2)] / int(match.group(1))
