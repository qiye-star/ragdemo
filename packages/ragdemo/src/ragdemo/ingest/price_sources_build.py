"""按环境变量拼出 `PriceSources`（Dagster 资源与 `facts prices` 回填命令共用）。

行情三层（ADR-0012）：Tushare MCP → Tushare REST → Wind 日 K 线，都经出网代理
（凭据由代理注入）。哪一层的地址没配，哪一层就不参与；三个都没配时退回 Mock 网关——
与 `parser_resource` 判断 `TEXTIN_BASE_URL` 同一套规则，本地开发与测试不该被逼着配真实地址。
冷却状态在配了真实来源时存 `core.source_health`（autocommit 连接，入库回滚撤销不了它）。
"""

from __future__ import annotations

import os

import psycopg

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.adapters.mcp_gateway import gateway_client_from_env
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.ingest.prices import PriceSources
from ragdemo.sources.health import MemorySourceHealth, PgSourceHealth

TUSHARE_MCP_URL_ENV = "RAGDEMO_TUSHARE_MCP_URL"
TUSHARE_REST_URL_ENV = "RAGDEMO_TUSHARE_REST_URL"
WIND_STOCK_URL_ENV = "RAGDEMO_WIND_MCP_URL"


def _configured(env: str) -> bool:
    return bool(os.environ.get(env, "").strip())


def build_price_sources() -> PriceSources:
    if not any(map(_configured, (TUSHARE_MCP_URL_ENV, TUSHARE_REST_URL_ENV, WIND_STOCK_URL_ENV))):
        return PriceSources(health=MemorySourceHealth(), tushare_mcp=MockMcpGateway())
    rest = None
    if _configured(TUSHARE_REST_URL_ENV):
        rest = HttpClient(
            "tushare-rest",
            os.environ[TUSHARE_REST_URL_ENV].strip(),
            policy=RetryPolicy(max_attempts=2, backoff_base_s=1.0),
            bucket=TokenBucket(rate_per_minute=120),
            timeout_s=30.0,
        )
    return PriceSources(
        health=PgSourceHealth(psycopg.connect(os.environ["RAGDEMO_DSN"], autocommit=True)),
        tushare_mcp=gateway_client_from_env(url_env=TUSHARE_MCP_URL_ENV, provider="tushare-mcp")
        if _configured(TUSHARE_MCP_URL_ENV)
        else None,
        tushare_rest=rest,
        wind_stock=gateway_client_from_env(url_env=WIND_STOCK_URL_ENV, provider="wind-mcp")
        if _configured(WIND_STOCK_URL_ENV)
        else None,
    )
