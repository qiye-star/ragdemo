"""按层路由：主层可用就用主层；失败按类别冷却并切下一层；全部失败要明确抛出。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ragdemo.adapters.errors import ProviderRefused, UpstreamUnavailable
from ragdemo.adapters.wind import WindResponseRejected
from ragdemo.sources.failures import FailureKind
from ragdemo.sources.health import MemorySourceHealth
from ragdemo.sources.router import AllLayersFailed, Layer, SourceRouter
from ragdemo.sources.routing import Capability

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
CAP = Capability.PRICE_DAILY


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _ok(value: str) -> Layer[str]:
    return Layer(name=f"ok-{value}", source=value, degraded=False, run=lambda: value)


def _fail(name: str, exc: Exception, calls: list[str] | None = None) -> Layer[str]:
    def run() -> str:
        if calls is not None:
            calls.append(name)
        raise exc

    return Layer(name=name, source=name, degraded=False, run=run)


def _router() -> tuple[SourceRouter, MemorySourceHealth, Clock]:
    health, clock = MemorySourceHealth(), Clock()
    return SourceRouter(health, clock=clock), health, clock


def test_primary_layer_wins_when_it_works() -> None:
    router, health, _ = _router()
    routed = router.run(CAP, [_ok("tushare"), _ok("wind")])
    assert (routed.value, routed.layer, routed.degraded, routed.skipped) == (
        "tushare",
        "ok-tushare",
        False,
        (),
    )
    assert health.last_success("ok-tushare", CAP) == NOW


def test_failure_falls_through_and_reports_why() -> None:
    router, _, _ = _router()
    wind = Layer(name="wind-kline", source="wind", degraded=True, run=lambda: "wind")
    routed = router.run(CAP, [_fail("tushare-mcp", UpstreamUnavailable("超时")), wind])
    assert routed.value == "wind" and routed.source == "wind" and routed.degraded
    assert routed.skipped[0][0] == "tushare-mcp" and "unavailable" in routed.skipped[0][1]


def test_layer_in_cooldown_is_not_called_until_it_expires() -> None:
    router, _, clock = _router()
    calls: list[str] = []
    hourly = ProviderRefused("抱歉，您访问接口(daily)频率超限(1次/小时)")
    layers = [_fail("tushare-mcp", hourly, calls), _ok("rest")]
    router.run(CAP, layers)
    clock.now = NOW + timedelta(minutes=59)
    routed = router.run(CAP, layers)
    assert calls == ["tushare-mcp"], "冷却期内不该再打一次必然被拒的请求"
    assert "冷却" in routed.skipped[0][1]
    clock.now = NOW + timedelta(hours=1, seconds=1)
    router.run(CAP, layers)
    assert calls == ["tushare-mcp", "tushare-mcp"]


def test_rejected_response_does_not_cool_the_layer_down() -> None:
    router, _, _ = _router()
    calls: list[str] = []
    layers = [_fail("wind", WindResponseRejected("形状不对"), calls), _ok("ifind")]
    router.run(CAP, layers)
    router.run(CAP, layers)
    assert calls == ["wind", "wind"]


def test_all_layers_failing_raises_with_every_reason() -> None:
    router, _, _ = _router()
    with pytest.raises(AllLayersFailed) as info:
        router.run(
            CAP,
            [
                _fail("a", ProviderRefused("抱歉，您没有接口(daily)访问权限")),
                _fail("b", UpstreamUnavailable("连不上")),
            ],
        )
    assert [name for name, _ in info.value.attempts] == ["a", "b"]
    assert info.value.kinds == {FailureKind.PERMISSION, FailureKind.UNAVAILABLE}


def test_programming_errors_are_not_swallowed_as_degradation() -> None:
    """KeyError 之类是我们的 bug，不是供应商不可用——降级会把 bug 藏起来。"""
    router, _, _ = _router()
    with pytest.raises(KeyError):
        router.run(CAP, [_fail("a", KeyError("close")), _ok("b")])
