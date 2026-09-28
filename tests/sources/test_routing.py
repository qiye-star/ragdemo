"""路由表就是分工表（ADR-0012）。这里守的是"分工明确"本身，不是某个实现细节。"""

from __future__ import annotations

from collections import Counter

from ragdemo.sources.routing import ROUTES, Capability


def test_every_mcp_service_has_at_most_one_primary_duty() -> None:
    primaries = Counter(layers[0].service for layers in ROUTES.values())
    assert all(n == 1 for n in primaries.values()), primaries


def test_duties_match_adr_0012() -> None:
    assert {cap: layers[0].service for cap, layers in ROUTES.items()} == {
        Capability.PRICE_DAILY: "tushare-mcp",
        Capability.FINANCIALS: "wind-stock-data",
        Capability.FINANCIALS_VERIFY: "ifind-stock",
        Capability.ANNOUNCEMENTS: "wind-financial-docs",
    }
    assert [s.name for s in ROUTES[Capability.PRICE_DAILY]] == [
        "tushare-mcp",
        "tushare-rest",
        "wind-kline",
    ]
    assert [s.name for s in ROUTES[Capability.ANNOUNCEMENTS]] == ["wind-docs", "ifind-notice"]


def test_financials_never_fall_back_to_another_source() -> None:
    """写入层现行值与来源无关：跨源写入会把精度差记成伪更正。"""
    assert len(ROUTES[Capability.FINANCIALS]) == 1


def test_verification_layers_never_write() -> None:
    assert not any(s.writes for s in ROUTES[Capability.FINANCIALS_VERIFY])


def test_cross_provider_fallbacks_are_marked_degraded() -> None:
    for layers in ROUTES.values():
        primary = layers[0].source
        for spec in layers[1:]:
            assert spec.degraded == (spec.source != primary), spec
