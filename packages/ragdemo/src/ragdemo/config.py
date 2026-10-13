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
from typing import Final

from ragdemo.parse.tiered.types import SkipRuleId
from ragdemo.parse.tiered.types import parse_skip_rules as _parse_skip_rules

DEFAULT_BLOB_ROOT = "data/blob"
DEFAULT_MAX_PAGES_PER_RUN = 500
# docs/06-retrieval.md §5：重排模型定死为 bge-reranker-v2-m3；ADR-0004：
# 嵌入维度固定 1024，bge-m3 是默认选择（Qwen3-Embedding 经 MRL 降到 1024
# 后可互换，但换模型是数据回填决定，不是这里该猜的事）。
DEFAULT_SILICONFLOW_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_SILICONFLOW_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_MODEL_NAME = "deepseek-chat"
DEFAULT_DESCRIBE_PROMPT = "v3"

# 环境变量里"真"的拼写变体。没有出现在这里的一律按 False 处理——
# 包括空字符串（未设置等价于默认值 False，而不是报错）。
_TRUTHY = frozenset({"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"})


# TextIn xParse 记账单价（人民币/页）。实测值，2026-10-13：34.84 元 / 257 页 = 0.1356；
# holdout2 抽检 51 页实付约 6.92 元（同口径 0.1357）。此前记账按 0.04 元/页，低估约 3.4 倍。
# 这是实测估算而非合同价：**须以供应商发票复核**后用环境变量 TEXTIN_COST_PER_PAGE_CNY 覆盖。
# 存储在 provider_snapshot.cost_cents（numeric(10,4)，单位分）：页数 × 单价 × 100 精确到 0.0001 分，
# 不做取整（13.56 分/页原样入账）。
DEFAULT_TEXTIN_COST_PER_PAGE_CNY = Decimal("0.1356")

# 起步期数据 + 模型每月预算上限（docs/00-overview.md §3 约束，docs/04-ingestion.md §5.3）。
DEFAULT_MONTHLY_BUDGET_CNY = Decimal(5000)


_ROUTABLE_DOC_TYPES: Final = frozenset({"announcement", "research_report"})
_WHOLE_DOC_TYPES: Final = frozenset({"annual_report", "quarterly"})
# 裁决（2026-10-13）：L0 v2 未通过 G9（阅读顺序人工抽查），不启用。开关保留为"永远只能是假"，
# 配置加载与 with_tiered_routing 都拒绝真值；重开要新增 ADR / 重开记录，不是改这个常量。
L0_REFUSED_MESSAGE: Final = "L0 v2 not validated (G9)"


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
    monthly_budget_cny: Decimal = DEFAULT_MONTHLY_BUDGET_CNY
    # 生成模型（表格描述等，阶段 3.1）。地址指向回环反向代理；不设置则用 Mock 描述器。
    model_base_url: str | None = None
    model_name: str = DEFAULT_MODEL_NAME
    describe_prompt: str = DEFAULT_DESCRIBE_PROMPT
    # L0 + TextIn 分页接入（docs/10.12-plan/2026-10-13-L0加TextIn分页接入与减页计划.md）。默认全关。
    # parse_tier_l0_enabled 只能为 False（见 L0_REFUSED_MESSAGE）；减页规则是 shadow_only，
    # 只有显式列在 parse_skip_rules 里的才会真的跳页。
    parse_tiered_enabled: bool = False
    parse_tiered_doc_types: frozenset[str] = frozenset()
    parse_tier_l0_enabled: bool = False
    parse_skip_rules: frozenset[SkipRuleId] = frozenset()

    def validate(self) -> None:
        if self.parse_tier_l0_enabled:
            raise ConfigError(f"PARSE_TIER_L0_ENABLED=true 被拒绝：{L0_REFUSED_MESSAGE}")
        whole = sorted(self.parse_tiered_doc_types & _WHOLE_DOC_TYPES)
        if whole:
            raise ConfigError(
                f"{whole} 永远整份走 TextIn（ADR-0008），不能放进 PARSE_TIERED_DOC_TYPES"
            )
        unknown = sorted(self.parse_tiered_doc_types - _ROUTABLE_DOC_TYPES)
        if unknown:
            raise ConfigError(
                "环境变量 PARSE_TIERED_DOC_TYPES 只允许 "
                f"{sorted(_ROUTABLE_DOC_TYPES)}，收到 {unknown}"
            )
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
        budget`）时才调用这个方法。单价默认取
        `DEFAULT_TEXTIN_COST_PER_PAGE_CNY`（实测值，待发票复核），环境变量覆盖；
        只有显式构造 `textin_cost_per_page_cny=None` 的配置才会走到下面的报错。
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
        textin_cost_per_page_cny = DEFAULT_TEXTIN_COST_PER_PAGE_CNY

    siliconflow_base_url = source.get("SILICONFLOW_BASE_URL") or None
    siliconflow_embed_model = (
        source.get("SILICONFLOW_EMBED_MODEL") or DEFAULT_SILICONFLOW_EMBED_MODEL
    )
    siliconflow_rerank_model = (
        source.get("SILICONFLOW_RERANK_MODEL") or DEFAULT_SILICONFLOW_RERANK_MODEL
    )

    raw_budget = (source.get("RAGDEMO_MONTHLY_BUDGET_CNY") or "").strip()
    try:
        monthly_budget_cny = Decimal(raw_budget) if raw_budget else DEFAULT_MONTHLY_BUDGET_CNY
    except InvalidOperation as exc:
        raise ConfigError(
            f"环境变量 RAGDEMO_MONTHLY_BUDGET_CNY 必须是数字，收到 {raw_budget!r}"
        ) from exc
    if monthly_budget_cny <= 0:
        raise ConfigError(f"环境变量 RAGDEMO_MONTHLY_BUDGET_CNY 必须为正数，收到 {raw_budget!r}")

    raw_types = source.get("PARSE_TIERED_DOC_TYPES") or ""
    try:
        skip_rules = _parse_skip_rules(source.get("PARSE_SKIP_RULES") or "")
    except ValueError as exc:
        raise ConfigError(f"环境变量 PARSE_SKIP_RULES：{exc}") from exc

    cfg = RagdemoConfig(
        blob_root=blob_root,
        textin_base_url=textin_base_url,
        textin_allow_private=textin_allow_private,
        textin_max_pages_per_run=textin_max_pages_per_run,
        textin_cost_per_page_cny=textin_cost_per_page_cny,
        siliconflow_base_url=siliconflow_base_url,
        siliconflow_embed_model=siliconflow_embed_model,
        siliconflow_rerank_model=siliconflow_rerank_model,
        monthly_budget_cny=monthly_budget_cny,
        model_base_url=source.get("MODEL_BASE_URL") or None,
        model_name=source.get("MODEL_NAME") or DEFAULT_MODEL_NAME,
        describe_prompt=source.get("DESCRIBE_PROMPT_VERSION") or DEFAULT_DESCRIBE_PROMPT,
        parse_tiered_enabled=(source.get("PARSE_TIERED_ENABLED") or "").strip() in _TRUTHY,
        parse_tiered_doc_types=frozenset(s.strip() for s in raw_types.split(",") if s.strip()),
        parse_tier_l0_enabled=(source.get("PARSE_TIER_L0_ENABLED") or "").strip() in _TRUTHY,
        parse_skip_rules=skip_rules,
    )
    cfg.validate()
    return cfg
