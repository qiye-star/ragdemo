"""硅基流动（SiliconFlow）客户端构造——嵌入与重排的宿主 API（adr/0004）。

这个模块只做一件事：把 `config/providers.yaml` 的 `siliconflow` 段与调用方
传入的 `base_url` 拼成一个 `HttpClient`。真正的嵌入/重排协议映射
（请求体形状、响应字段、L2 归一化）分别在 `embed/siliconflow.py` 与
`retrieval/rerank_siliconflow.py` 里，不在这里——这里没有任何供应商 SDK，
也没有任何读取密钥的代码路径，密钥只作为 `EMBEDDING_API_KEY` 存在于
`infra/docker-compose.yml` 的 egress-proxy 服务环境变量里，由 mitmproxy
的 `_inject()` 以 bearer 头注入，Python 侧永远拿不到它（`CLAUDE.md` §3）。

`base_url` 由调用方传入而不是直接取 yaml 里的 `base_url` 字段：yaml 里那个
是生产域名 `api.siliconflow.cn`，只作为限流/配额/超时参数的唯一来源；实际
连接地址是回环反向代理（`SILICONFLOW_BASE_URL`，见 `config.py::
require_siliconflow_base_url`），Host 头改写成 `api.siliconflow.cn` 后
仍命中出网代理白名单里已有的那条 bearer 注入规则
（`infra/egress-proxy/policy.yaml`）。
"""

from __future__ import annotations

import httpx

from ragdemo.adapters.http import HttpClient, ProviderConfig, TokenBucket

PROVIDER = "siliconflow"
EMBEDDINGS_ENDPOINT = "/v1/embeddings"
RERANK_ENDPOINT = "/v1/rerank"


def client_from_config(
    base_url: str, *, transport: httpx.BaseTransport | None = None
) -> HttpClient:
    """按 `config/providers.yaml` 的 `siliconflow` 段构造客户端。

    `transport` 只在测试里传（`httpx.MockTransport`），生产路径留空走真实网络。
    """
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
