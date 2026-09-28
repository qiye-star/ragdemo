"""适配器错误类型。"""

from __future__ import annotations


class AdapterError(RuntimeError):
    """适配器层的基类错误。"""


class RateLimited(AdapterError):
    """被上游限速。retry_after_s 来自 Retry-After 响应头，没有则为 None。"""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExceeded(AdapterError):
    """超出本日或本月配额。不重试——重试只会继续烧钱。"""


class UpstreamUnavailable(AdapterError):
    """重试耗尽后仍失败。抛出它会让 Dagster 分区失败，这是刻意的：
    部分数据入库比没数据更糟，下游会以为数据完整。"""


class ProviderRefused(AdapterError):
    """供应商收到了请求并明确拒绝（无权限、限频）。与 `UpstreamUnavailable` 分开：
    拒绝原因在报文里，决定了该冷却多久（ADR-0012），而且重试同一请求没有用。"""
