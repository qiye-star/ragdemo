"""按层尝试，失败归类、冷却、切下一层（ADR-0012）。

`Layer.run` 应当包含"取数 + 解析"：解析校验不过（响应被拒）也要能切到下一层，
而不是把一份不可信的数据交给调用方。取数时记的 provider_snapshot 不受影响——
被拒的原始响应照样留底。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeVar

from ragdemo.adapters.errors import AdapterError
from ragdemo.sources.failures import Failure, FailureKind, classify
from ragdemo.sources.health import SourceHealth
from ragdemo.sources.routing import Capability

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class Layer(Generic[T]):
    name: str
    source: str
    degraded: bool
    run: Callable[[], T]


@dataclass(frozen=True)
class Routed(Generic[T]):
    value: T
    layer: str
    source: str
    degraded: bool
    skipped: tuple[tuple[str, str], ...]  # (层名, 为什么没用它)


class AllLayersFailed(AdapterError):
    def __init__(
        self, capability: Capability, attempts: Sequence[tuple[str, Failure | None]]
    ) -> None:
        self.capability = capability
        self.attempts = list(attempts)
        detail = "；".join(
            f"{name}: {'冷却中' if f is None else f'{f.kind.value} {f.message[:120]}'}"
            for name, f in self.attempts
        )
        super().__init__(f"{capability} 所有层都不可用（{detail}）")

    @property
    def kinds(self) -> set[FailureKind]:
        return {f.kind for _, f in self.attempts if f is not None}


class SourceRouter:
    def __init__(
        self, health: SourceHealth, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._health = health
        self._clock = clock

    def run(self, capability: Capability, layers: Sequence[Layer[T]]) -> Routed[T]:
        skipped: list[tuple[str, str]] = []
        attempts: list[tuple[str, Failure | None]] = []
        for layer in layers:
            now = self._clock()
            until = self._health.blocked_until(layer.name, capability)
            if until is not None and until > now:
                skipped.append((layer.name, f"冷却中，至 {until.isoformat()}"))
                attempts.append((layer.name, None))
                continue
            try:
                value = layer.run()
            except AdapterError as exc:
                failure = classify(exc)
                self._health.record_failure(layer.name, capability, failure, now=self._clock())
                skipped.append((layer.name, f"{failure.kind.value}: {failure.message[:200]}"))
                attempts.append((layer.name, failure))
                logger.warning(
                    "数据源层失败，切下一层",
                    extra={
                        "capability": str(capability),
                        "layer": layer.name,
                        "failure_kind": failure.kind.value,
                        "cooldown_s": failure.cooldown.total_seconds(),
                    },
                )
                continue
            self._health.record_success(layer.name, capability, now=self._clock())
            if skipped or layer.degraded:
                logger.warning(
                    "数据源已降级",
                    extra={
                        "capability": str(capability),
                        "layer": layer.name,
                        "degraded": layer.degraded,
                        "skipped": [name for name, _ in skipped],
                    },
                )
            return Routed(value, layer.name, layer.source, layer.degraded, tuple(skipped))
        raise AllLayersFailed(capability, attempts)
