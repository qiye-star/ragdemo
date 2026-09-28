"""路由表就是分工表（ADR-0012，公告分工被 ADR-0013、以及 2026-09-28 公告双源接入计划修订）。
这里守的是"分工明确"本身，不是某个实现细节。"""

from __future__ import annotations

from collections import Counter

from ragdemo.sources.routing import ROUTES, Capability


def test_every_mcp_service_has_at_most_one_primary_duty() -> None:
    primaries = Counter(layers[0].service for layers in ROUTES.values())
    assert all(n == 1 for n in primaries.values()), primaries


def test_duties_match_adr_0012_and_0013() -> None:
    assert {cap: layers[0].service for cap, layers in ROUTES.items()} == {
        Capability.PRICE_DAILY: "tushare-mcp",
        Capability.FINANCIALS: "wind-stock-data",
        Capability.FINANCIALS_VERIFY: "ifind-stock",
        Capability.ANNOUNCEMENTS_CN: "cninfo-http",
        Capability.ANNOUNCEMENTS_HK: "hkexnews-http",
        Capability.ANNOUNCEMENTS_US: "edgar-http",
        Capability.ANNOUNCEMENTS_VERIFY: "wind-financial-docs",
    }
    assert [s.name for s in ROUTES[Capability.PRICE_DAILY]] == [
        "tushare-mcp",
        "tushare-rest",
        "wind-kline",
    ]
    assert [s.name for s in ROUTES[Capability.ANNOUNCEMENTS_CN]] == [
        "cninfo",
        "wind-docs",
        "ifind-notice",
    ]


def test_financials_never_fall_back_to_another_source() -> None:
    """写入层现行值与来源无关：跨源写入会把精度差记成伪更正。"""
    assert len(ROUTES[Capability.FINANCIALS]) == 1


def test_announcement_fallbacks_never_claim_full_text() -> None:
    """A 股降级层（Wind/iFind）只给摘录/片段，进不了要求全文的文档管线。"""
    primary, *fallbacks = ROUTES[Capability.ANNOUNCEMENTS_CN]
    assert primary.full_text
    assert fallbacks and all(not spec.full_text for spec in fallbacks)


def test_hk_and_us_announcements_have_no_fallback_layer() -> None:
    """披露易、EDGAR 都是官方渠道：不可用就整批失败，不需要也不该降级。"""
    assert len(ROUTES[Capability.ANNOUNCEMENTS_HK]) == 1
    assert len(ROUTES[Capability.ANNOUNCEMENTS_US]) == 1
    assert ROUTES[Capability.ANNOUNCEMENTS_HK][0].full_text
    assert ROUTES[Capability.ANNOUNCEMENTS_US][0].full_text


def test_verification_layers_never_write() -> None:
    assert not any(s.writes for s in ROUTES[Capability.FINANCIALS_VERIFY])
    assert not any(s.writes for s in ROUTES[Capability.ANNOUNCEMENTS_VERIFY])


def test_cross_provider_fallbacks_are_marked_degraded() -> None:
    for layers in ROUTES.values():
        primary = layers[0].source
        for spec in layers[1:]:
            assert spec.degraded == (spec.source != primary), spec
