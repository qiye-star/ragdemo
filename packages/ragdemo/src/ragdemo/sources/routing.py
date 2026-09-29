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
    # 公告按市场分三条能力（2026-09-28 公告双源接入计划，修订 ADR-0013）：每个市场的
    # 官方/半官方渠道形状不同（A 股巨潮日精度、港股披露易分钟精度、美股 EDGAR 秒精度），
    # 合并成一个 Capability 会强迫写一套通用接口，掩盖这些真实差异。
    ANNOUNCEMENTS_CN = "announcements_cn"
    ANNOUNCEMENTS_HK = "announcements_hk"
    ANNOUNCEMENTS_US = "announcements_us"


@dataclass(frozen=True)
class LayerSpec:
    name: str  # 层名：日志、core.source_health 里用它
    service: str  # 哪个 MCP 服务 / 通道
    source: str  # 写库时的来源标识
    degraded: bool
    writes: bool = True
    full_text: bool = True  # False：只给目录/摘录，进不了要求全文的文档管线


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
    # A 股：巨潮资讯网为主（官方指定披露平台，能给全文），iFind 降级只写目录，
    # 全文进待补清单（2026-09-28 公告双源接入计划，修订 ADR-0013 的"只有一层"）。
    # Wind 曾经也是一层（`wind-financial-docs`/`get_company_announcements`），
    # 但 2026-09-29 实盘验收发现这个账号的 Wind 服务范围只覆盖财务/行情/股东/
    # 分部数据，不包含公告——移除，不是代码缺陷（ADR-0013 修订）。
    Capability.ANNOUNCEMENTS_CN: (
        LayerSpec("cninfo", service="cninfo-http", source="cninfo", degraded=False),
        LayerSpec(
            "ifind-notice", service="ifind-news", source="ifind", degraded=True, full_text=False
        ),
    ),
    # 港股：港交所披露易（官方指定披露平台），单层——不可用就整批失败，不悄悄降级到
    # 给不出全文的来源。
    Capability.ANNOUNCEMENTS_HK: (
        LayerSpec("hkexnews", service="hkexnews-http", source="hkexnews", degraded=False),
    ),
    # 美股 + 已在 SEC 报送的 ADR（台积电、日月光、阿里巴巴、百度、金山云）：EDGAR，单层。
    Capability.ANNOUNCEMENTS_US: (
        LayerSpec("edgar", service="edgar-http", source="edgar", degraded=False),
    ),
}


def layer_spec(capability: Capability, name: str) -> LayerSpec:
    for spec in ROUTES[capability]:
        if spec.name == name:
            return spec
    raise KeyError(f"{capability} 的分工表里没有层 {name!r}")
