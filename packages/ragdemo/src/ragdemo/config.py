"""运行期环境配置。

读取 `RAGDEMO_BLOB_ROOT` / `TEXTIN_*`。缺失必填值时像 `cli.py::_dsn()` 一样
大声失败——错误信息点名哪个变量、指向 `.env.example`。

`TEXTIN_BASE_URL` **没有默认值**，但 `load_config()` 本身不会因为它缺失就
报错：dry run 与 `--parser mock` 都不需要它，逼着每次加载配置都设置它会让
离线开发寸步难行。真正要构造 `TextInParser`（即将花钱）的调用点必须显式调
`require_textin_base_url()`，在那一刻才大声失败——这与 `TEXTIN_MAX_PAGES_
PER_RUN` 之类"有默认值、格式错就报错"的字段是两种不同的校验时机，不能
混为一谈。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_BLOB_ROOT = "data/blob"
DEFAULT_MAX_PAGES_PER_RUN = 500
# docs/06-retrieval.md §5：重排模型定死为 bge-reranker-v2-m3；ADR-0004：
# 嵌入维度固定 1024，bge-m3 是默认选择（Qwen3-Embedding 经 MRL 降到 1024
# 后可互换，但换模型是数据回填决定，不是这里该猜的事）。
DEFAULT_SILICONFLOW_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_SILICONFLOW_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# 环境变量里"真"的拼写变体。没有出现在这里的一律按 False 处理——
# 包括空字符串（未设置等价于默认值 False，而不是报错）。
_TRUTHY = frozenset({"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"})


class ConfigError(RuntimeError):
    """必填的环境配置缺失或格式不合法。"""


@dataclass(frozen=True)
class RagdemoConfig:
    blob_root: Path
    textin_base_url: str | None
    textin_allow_private: bool
    textin_max_pages_per_run: int
    textin_cost_per_page_cny: Decimal | None
    siliconflow_base_url: str | None
    siliconflow_embed_model: str
    siliconflow_rerank_model: str

    def validate(self) -> None:
        if self.textin_max_pages_per_run <= 0:
            raise ConfigError(
                "环境变量 TEXTIN_MAX_PAGES_PER_RUN 必须是正整数，"
                f"收到 {self.textin_max_pages_per_run}"
            )

    def require_textin_base_url(self) -> str:
        """只有真正要构造 `TextInParser`（即将产生计费调用）时才调用这个方法。

        dry run 与 `--parser mock` 都不经过这里——逼着它们也要求这个变量，
        会让离线开发与测试寸步难行。
        """
        if not self.textin_base_url:
            raise ConfigError("环境变量 TEXTIN_BASE_URL 未设置（参见 .env.example）")
        return self.textin_base_url

    def require_textin_cost_per_page_cny(self) -> Decimal:
        """只有真正要判定 C 档月度预算（`parse/router.py::check_monthly_
        budget`）时才调用这个方法。C 档单价是商务合同条款，不该在代码里
        编一个默认数字——没有配置就应该让调用方明确知道"预算判断做不了"，
        而不是悄悄用一个瞎猜的价格算出一个看起来正常的数字。
        """
        if self.textin_cost_per_page_cny is None:
            raise ConfigError("环境变量 TEXTIN_COST_PER_PAGE_CNY 未设置（参见 .env.example）")
        return self.textin_cost_per_page_cny

    def require_siliconflow_base_url(self) -> str:
        """只有真正要构造 SiliconFlowEmbedder/SiliconFlowReranker 时才调用。

        没有默认值的理由与 `require_textin_base_url` 完全一致：写死一个
        公网地址会让每一份 clone 默认把查询数据发到境外服务，违反
        `CLAUDE.md` §0「数据境内存储」。正确值指向回环反向代理
        （`infra/docker-compose.yml` 的 8082 端口），不是 api.siliconflow.cn 本身。
        """
        if not self.siliconflow_base_url:
            raise ConfigError("环境变量 SILICONFLOW_BASE_URL 未设置（参见 .env.example）")
        return self.siliconflow_base_url


def load_config(env: Mapping[str, str] | None = None) -> RagdemoConfig:
    """从环境变量（或显式传入的映射，供测试用）构造配置并校验。"""
    source: Mapping[str, str] = env if env is not None else os.environ

    blob_root = Path(source.get("RAGDEMO_BLOB_ROOT") or DEFAULT_BLOB_ROOT)
    textin_base_url = source.get("TEXTIN_BASE_URL") or None
    textin_allow_private = (source.get("TEXTIN_ALLOW_PRIVATE") or "").strip() in _TRUTHY

    raw_max_pages = (source.get("TEXTIN_MAX_PAGES_PER_RUN") or "").strip()
    if raw_max_pages:
        try:
            textin_max_pages_per_run = int(raw_max_pages)
        except ValueError as exc:
            raise ConfigError(
                f"环境变量 TEXTIN_MAX_PAGES_PER_RUN 必须是整数，收到 {raw_max_pages!r}"
            ) from exc
    else:
        textin_max_pages_per_run = DEFAULT_MAX_PAGES_PER_RUN

    raw_cost_per_page = (source.get("TEXTIN_COST_PER_PAGE_CNY") or "").strip()
    textin_cost_per_page_cny: Decimal | None
    if raw_cost_per_page:
        try:
            textin_cost_per_page_cny = Decimal(raw_cost_per_page)
        except InvalidOperation as exc:
            raise ConfigError(
                f"环境变量 TEXTIN_COST_PER_PAGE_CNY 必须是数字，收到 {raw_cost_per_page!r}"
            ) from exc
    else:
        textin_cost_per_page_cny = None

    siliconflow_base_url = source.get("SILICONFLOW_BASE_URL") or None
    siliconflow_embed_model = (
        source.get("SILICONFLOW_EMBED_MODEL") or DEFAULT_SILICONFLOW_EMBED_MODEL
    )
    siliconflow_rerank_model = (
        source.get("SILICONFLOW_RERANK_MODEL") or DEFAULT_SILICONFLOW_RERANK_MODEL
    )

    cfg = RagdemoConfig(
        blob_root=blob_root,
        textin_base_url=textin_base_url,
        textin_allow_private=textin_allow_private,
        textin_max_pages_per_run=textin_max_pages_per_run,
        textin_cost_per_page_cny=textin_cost_per_page_cny,
        siliconflow_base_url=siliconflow_base_url,
        siliconflow_embed_model=siliconflow_embed_model,
        siliconflow_rerank_model=siliconflow_rerank_model,
    )
    cfg.validate()
    return cfg
