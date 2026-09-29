"""按配置选表格描述器：配了模型地址就用真实的，没配退回 Mock。

与 `parser_resource` 判断 `TEXTIN_BASE_URL`、`price_sources` 判断行情地址是同一套规则——
本地开发与测试不该被逼着配齐模型才能跑通。Mock 只留给测试与离线开发；生产 / 真实入库
必须配 `MODEL_BASE_URL`（指向回环反向代理，见 `infra/docker-compose.yml` 的 8086）。
"""

from __future__ import annotations

import httpx

from ragdemo.adapters.http import HttpClient, ProviderConfig, TokenBucket
from ragdemo.config import RagdemoConfig
from ragdemo.parse.describe import MockTableDescriber, TableDescriber
from ragdemo.parse.describe_llm import LlmTableDescriber

PROVIDER = "deepseek"


def deepseek_client(base_url: str, *, transport: httpx.BaseTransport | None = None) -> HttpClient:
    cfg = ProviderConfig.load(PROVIDER)
    return HttpClient(
        provider=PROVIDER,
        base_url=base_url,
        policy=cfg.retry,
        bucket=TokenBucket(rate_per_minute=cfg.rate_limit_qpm),
        timeout_s=cfg.timeout_s,
        daily_quota=cfg.daily_quota,
        cost_per_call_cents=cfg.cost_per_call_cents,
        transport=transport,
    )


def build_describer(cfg: RagdemoConfig) -> TableDescriber:
    if not cfg.model_base_url:
        return MockTableDescriber()
    return LlmTableDescriber(
        deepseek_client(cfg.model_base_url),
        model=cfg.model_name,
        prompt_version=cfg.describe_prompt,
    )
