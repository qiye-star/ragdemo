"""带限流、重试与配额的 HTTP 客户端。

只重试幂等的读操作。429 优先读 Retry-After，没有才用指数退避。
重试耗尽抛 UpstreamUnavailable —— 让 Dagster 分区失败，而不是返回部分数据。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml

from ragdemo.adapters.base import RawResponse
from ragdemo.adapters.errors import QuotaExceeded, RateLimited, UpstreamUnavailable

DEFAULT_CONFIG_PATH = Path("config/providers.yaml")

# httpx 默认在 INFO 级别打印完整请求行（含查询字符串），例如
# "HTTP Request: GET .../income?token=sk-xxx&... "HTTP/1.1 200 OK""。
# CLAUDE.md §3：密钥不得出现在日志中。今天没有适配器往 params 里塞 token
# （鉴权推迟到 P1c 的出网代理），但一旦有人加了一个 token 参数，
# httpx 的默认日志会把它原样写进日志——这里提前把 httpx 自己的 logger
# 降到 WARNING，永久堵死这条口子，不依赖调用方记得脱敏。
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_base_s: float = 1.0
    retry_on_status: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    rate_limit_qpm: int
    daily_quota: int
    timeout_s: float
    retry: RetryPolicy
    cost_per_call_cents: Decimal

    @staticmethod
    def load(provider: str, path: Path = DEFAULT_CONFIG_PATH) -> ProviderConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))[provider]
        retry = raw.get("retry", {})
        return ProviderConfig(
            base_url=raw["base_url"],
            rate_limit_qpm=int(raw["rate_limit_qpm"]),
            daily_quota=int(raw["daily_quota"]),
            timeout_s=float(raw.get("timeout_s", 30)),
            retry=RetryPolicy(
                max_attempts=int(retry.get("max_attempts", 4)),
                backoff_base_s=float(retry.get("backoff_base_s", 1.0)),
            ),
            cost_per_call_cents=Decimal(str(raw.get("cost_per_call_cents", 0))),
        )


@dataclass
class TokenBucket:
    """每分钟 rate_per_minute 个令牌的漏桶。acquire() 返回需要等待的秒数。"""

    rate_per_minute: int
    _allowance: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        self._allowance = 1.0
        self._last = time.monotonic()

    def acquire(self) -> float:
        now = time.monotonic()
        self._allowance = min(
            1.0,
            self._allowance + (now - self._last) * self.rate_per_minute / 60.0,
        )
        self._last = now
        if self._allowance >= 1.0:
            self._allowance -= 1.0
            return 0.0
        return (1.0 - self._allowance) * 60.0 / self.rate_per_minute


class HttpClient:
    """一个供应商一个实例。密钥由出网代理注入，这里不持有任何凭据。"""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        policy: RetryPolicy,
        bucket: TokenBucket,
        timeout_s: float = 30.0,
        daily_quota: int | None = None,
        cost_per_call_cents: Decimal = Decimal(0),
        transport: httpx.BaseTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        """`default_headers` 应用到这个客户端发出的**每一个**请求（httpx.Client
        在构造时合并进底层连接池，不需要每次调用都重复传）。加它的直接
        动因是 EDGAR：SEC 的 `data.sec.gov` / `www.sec.gov` 对没有可辨识
        User-Agent 的请求直接回 403（实测确认，不是文档猜测的）——这不是
        限流也不是配额，重试多少次都一样，之前完全没有代码路径设置过
        这个头，EDGAR 的两个真实主机此前都连不通。"""
        self.provider = provider
        self.policy = policy
        self.bucket = bucket
        self.daily_quota = daily_quota
        self.cost_per_call_cents = cost_per_call_cents
        self.calls_today = 0
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers=default_headers,
        )

    def get_json(self, endpoint: str, params: Mapping[str, Any]) -> RawResponse:
        if self.daily_quota is not None and self.calls_today >= self.daily_quota:
            raise QuotaExceeded(f"{self.provider} 已达每日配额 {self.daily_quota}")

        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            wait = self.bucket.acquire()
            if wait > 0:
                time.sleep(wait)

            try:
                response = self._client.get(endpoint, params=dict(params))
            except httpx.TransportError as exc:
                last_error = exc
            else:
                self.calls_today += 1
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except json.JSONDecodeError as e:
                        raise UpstreamUnavailable(
                            f"{self.provider} {endpoint} 返回无效 JSON 体"
                        ) from e
                    return RawResponse(
                        provider=self.provider,
                        endpoint=endpoint,
                        params=params,
                        payload=payload,
                        http_status=response.status_code,
                        fetched_at=datetime.now(UTC),
                        cost_cents=self.cost_per_call_cents,
                    )
                if response.status_code not in self.policy.retry_on_status:
                    raise UpstreamUnavailable(
                        f"{self.provider} {endpoint} 返回 {response.status_code}（不重试）"
                    )
                last_error = _error_for(response)

            if attempt < self.policy.max_attempts:
                time.sleep(_backoff_seconds(last_error, attempt, self.policy))

        raise UpstreamUnavailable(
            f"{self.provider} {endpoint} 重试 {self.policy.max_attempts} 次后仍失败"
        ) from last_error

    def get_bytes(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """GET 一个非 JSON 的二进制/文本资源（如 EDGAR 的申报全文 HTML）。

        与 `get_json` 共用限流/重试/配额，但不解析响应体——调用方拿到的是
        原始字节，自己决定怎么存（BlobStore）与怎么解析。不产出 `RawResponse`：
        `RawResponse.payload` 的既有约定是"可 JSON 序列化的结构化数据"
        （`ingest/snapshot.py::save_snapshot` 直接 `json.dumps` 它），塞一段
        任意二进制/HTML 进去会在落 `provider_snapshot` 时破坏这个约定。
        """
        if self.daily_quota is not None and self.calls_today >= self.daily_quota:
            raise QuotaExceeded(f"{self.provider} 已达每日配额 {self.daily_quota}")

        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            wait = self.bucket.acquire()
            if wait > 0:
                time.sleep(wait)

            try:
                response = self._client.get(endpoint, params=dict(params), headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                self.calls_today += 1
                if response.status_code < 400:
                    return response.content
                if response.status_code not in self.policy.retry_on_status:
                    raise UpstreamUnavailable(
                        f"{self.provider} {endpoint} 返回 {response.status_code}（不重试）"
                    )
                last_error = _error_for(response)

            if attempt < self.policy.max_attempts:
                time.sleep(_backoff_seconds(last_error, attempt, self.policy))

        raise UpstreamUnavailable(
            f"{self.provider} {endpoint} 重试 {self.policy.max_attempts} 次后仍失败"
        ) from last_error

    def post_text(
        self,
        endpoint: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RawResponse:
        """POST JSON 体，返回**原始响应文本**（payload 是 str）。

        与 get_json 有一处刻意的不同：它对任何 HTTP 状态码都返回 RawResponse，
        不因状态码抛异常。MCP 网关把业务含义放在响应体里——实测 HTTP 500 也带着
        一个可读的 JSON-RPC error（`search_stock` 稳定复现），状态码本身不足以
        分类，硬按状态码抛会把可诊断的上游故障变成一句「返回 500」。分类交给
        调用方，这一层只负责限流、重试、配额与计费。

        只有连传输层都没拿到响应（连不上、超时）才抛 UpstreamUnavailable。
        """
        if self.daily_quota is not None and self.calls_today >= self.daily_quota:
            raise QuotaExceeded(f"{self.provider} 已达每日配额 {self.daily_quota}")

        last_error: Exception | None = None
        last_response: httpx.Response | None = None
        merged = {"Content-Type": "application/json", **dict(headers or {})}

        for attempt in range(1, self.policy.max_attempts + 1):
            wait = self.bucket.acquire()
            if wait > 0:
                time.sleep(wait)

            try:
                response = self._client.post(endpoint, json=dict(body), headers=merged)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                self.calls_today += 1
                last_response = response
                if response.status_code not in self.policy.retry_on_status:
                    break
                last_error = _error_for(response)

            if attempt < self.policy.max_attempts:
                time.sleep(_backoff_seconds(last_error, attempt, self.policy))

        if last_response is None:
            raise UpstreamUnavailable(
                f"{self.provider} {endpoint} 重试 {self.policy.max_attempts} 次后仍未拿到响应"
            ) from last_error

        return RawResponse(
            provider=self.provider,
            endpoint=endpoint,
            params=body,
            payload=last_response.text,
            http_status=last_response.status_code,
            fetched_at=datetime.now(UTC),
            cost_cents=self.cost_per_call_cents,
        )


def _error_for(response: httpx.Response) -> Exception:
    if response.status_code == 429:
        header = response.headers.get("Retry-After")
        return RateLimited("上游限速", retry_after_s=float(header) if header else None)
    return UpstreamUnavailable(f"HTTP {response.status_code}")


def _backoff_seconds(error: Exception | None, attempt: int, policy: RetryPolicy) -> float:
    if isinstance(error, RateLimited) and error.retry_after_s is not None:
        return error.retry_after_s
    return policy.backoff_base_s * (2.0 ** (attempt - 1))
