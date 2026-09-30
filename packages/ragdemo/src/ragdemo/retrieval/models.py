"""按模式构造嵌入器与重排器（API、评测、`docs embed` 共用）。

`mock`：确定性 Mock，离线开发与测试用；`siliconflow`：真实的硅基流动 bge-m3 / bge-reranker
（经出网代理，`SILICONFLOW_BASE_URL` 指向回环反向代理，密钥只在代理层，adr/0004）。
"""

from __future__ import annotations

from ragdemo.adapters.siliconflow import client_from_config
from ragdemo.config import RagdemoConfig, load_config
from ragdemo.embed.base import Embedder
from ragdemo.embed.mock import MockEmbedder
from ragdemo.embed.siliconflow import SiliconFlowEmbedder
from ragdemo.retrieval.rerank import MockReranker, Reranker
from ragdemo.retrieval.rerank_siliconflow import SiliconFlowReranker

MODES = ("mock", "siliconflow")


def build_models(mode: str, config: RagdemoConfig | None = None) -> tuple[Embedder, Reranker]:
    if mode == "mock":
        return MockEmbedder(), MockReranker()
    if mode != "siliconflow":
        raise ValueError(f"未知的模型模式 {mode!r}，可选 {MODES}")
    cfg = config or load_config()
    # 嵌入与重排是同一个供应商——共用一个 HttpClient 实例，令牌桶 / 日配额因此正确地
    # 按供应商而不是按操作类型计数（http.py::HttpClient 的类 docstring）。
    http = client_from_config(cfg.require_siliconflow_base_url())
    return (
        SiliconFlowEmbedder(http, model=cfg.siliconflow_embed_model),
        SiliconFlowReranker(http, model=cfg.siliconflow_rerank_model),
    )
