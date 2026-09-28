"""分工表（ADR-0012）。改这张表就是改分工，要同步改 ADR。

`service` 是一个具体的 MCP 服务（或 REST 通道）；每个服务至多一个主职（某项数据的
第 1 层），在别的数据上出现只能是显式标注的降级层。`source` 是写入 `core.*` 时的
来源标识：同一供应商的另一条通道（Tushare MCP → REST）不算降级，换了供应商才算。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    PRICE_DAILY = "price_daily"
    FINANCIALS = "financials"
    FINANCIALS_VERIFY = "financials_verify"
    ANNOUNCEMENTS = "announcements"


@dataclass(frozen=True)
class LayerSpec:
    name: str  # 层名：日志、core.source_health 里用它
    service: str  # 哪个 MCP 服务 / 通道
    source: str  # 写库时的来源标识
    degraded: bool
    writes: bool = True


ROUTES: Mapping[Capability, tuple[LayerSpec, ...]] = {
    Capability.PRICE_DAILY: (
        LayerSpec("tushare-mcp", service="tushare-mcp", source="tushare", degraded=False),
        LayerSpec("tushare-rest", service="tushare-rest", source="tushare", degraded=False),
        LayerSpec("wind-kline", service="wind-stock-data", source="wind", degraded=True),
    ),
    # 只有一层：Wind 不可用时记入 core.ingest_backlog，下次触发补，不跨源写入。
    Capability.FINANCIALS: (
        LayerSpec("wind-fundamentals", service="wind-stock-data", source="wind", degraded=False),
    ),
    Capability.FINANCIALS_VERIFY: (
        LayerSpec(
            "ifind-financials", service="ifind-stock", source="ifind", degraded=False, writes=False
        ),
    ),
    Capability.ANNOUNCEMENTS: (
        LayerSpec("wind-docs", service="wind-financial-docs", source="wind", degraded=False),
        LayerSpec("ifind-notice", service="ifind-news", source="ifind", degraded=True),
    ),
}


def layer_spec(capability: Capability, name: str) -> LayerSpec:
    for spec in ROUTES[capability]:
        if spec.name == name:
            return spec
    raise KeyError(f"{capability} 的分工表里没有层 {name!r}")
