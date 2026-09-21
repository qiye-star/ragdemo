"""带限流、重试与配额的 HTTP 客户端。

只重试幂等的读操作。429 优先读 Retry-After，没有才用指数退避。
重试耗尽抛 UpstreamUnavailable —— 让 Dagster 分区失败，而不是返回部分数据。
"""
from __future__ import annotations

import json
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
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.bucket = bucket
        self.daily_quota = daily_quota
        self.cost_per_call_cents = cost_per_call_cents
        self.calls_today = 0
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout_s, transport=transport
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


def _error_for(response: httpx.Response) -> Exception:
    if response.status_code == 429:
        header = response.headers.get("Retry-After")
        return RateLimited(
            "上游限速", retry_after_s=float(header) if header else None
        )
    return UpstreamUnavailable(f"HTTP {response.status_code}")


def _backoff_seconds(error: Exception | None, attempt: int, policy: RetryPolicy) -> float:
    if isinstance(error, RateLimited) and error.retry_after_s is not None:
        return error.retry_after_s
    return policy.backoff_base_s * (2.0 ** (attempt - 1))
