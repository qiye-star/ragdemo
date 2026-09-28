"""各层的冷却状态。存 `core.source_health`（迁移 019），跨进程生效：Dagster 每个
run 是新进程，只放内存的话，被限频 1 次/小时的接口每个 run 都会再被拒一次。

`PgSourceHealth` 要一条 autocommit 连接：入库流程出错会 rollback 自己的事务，
冷却记录不能跟着被撤销——正是出错的时候最需要它。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

import psycopg

from ragdemo.sources.failures import Failure


class SourceHealth(Protocol):
    def blocked_until(self, layer: str, capability: str) -> datetime | None: ...
    def record_failure(
        self, layer: str, capability: str, failure: Failure, *, now: datetime
    ) -> None: ...
    def record_success(self, layer: str, capability: str, *, now: datetime) -> None: ...


@dataclass
class _State:
    blocked_until: datetime | None = None
    last_failure: Failure | None = None
    last_success: datetime | None = None


class MemorySourceHealth:
    """测试与单次 CLI 用。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _State] = {}

    def _state(self, layer: str, capability: str) -> _State:
        return self._states.setdefault((layer, capability), _State())

    def blocked_until(self, layer: str, capability: str) -> datetime | None:
        return self._state(layer, capability).blocked_until

    def record_failure(
        self, layer: str, capability: str, failure: Failure, *, now: datetime
    ) -> None:
        state = self._state(layer, capability)
        state.blocked_until = now + failure.cooldown
        state.last_failure = failure

    def record_success(self, layer: str, capability: str, *, now: datetime) -> None:
        state = self._state(layer, capability)
        state.blocked_until = None
        state.last_success = now

    def last_success(self, layer: str, capability: str) -> datetime | None:
        return self._state(layer, capability).last_success

    def last_failure(self, layer: str, capability: str) -> Failure | None:
        return self._state(layer, capability).last_failure


class PgSourceHealth:
    def __init__(self, conn: psycopg.Connection) -> None:
        if not conn.autocommit:
            raise ValueError("PgSourceHealth 需要 autocommit 连接：冷却记录不能随入库事务回滚")
        self._conn = conn

    def blocked_until(self, layer: str, capability: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT blocked_until FROM core.source_health WHERE layer = %s AND capability = %s",
            (layer, capability),
        ).fetchone()
        return None if row is None else cast(datetime | None, row[0])

    def record_failure(
        self, layer: str, capability: str, failure: Failure, *, now: datetime
    ) -> None:
        self._conn.execute(
            "INSERT INTO core.source_health (layer, capability, blocked_until,"
            " last_failure_kind, last_failure_message, last_failure_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (layer, capability) DO UPDATE SET"
            "  blocked_until = EXCLUDED.blocked_until,"
            "  last_failure_kind = EXCLUDED.last_failure_kind,"
            "  last_failure_message = EXCLUDED.last_failure_message,"
            "  last_failure_at = EXCLUDED.last_failure_at",
            (layer, capability, now + failure.cooldown, failure.kind.value, failure.message, now),
        )

    def record_success(self, layer: str, capability: str, *, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO core.source_health (layer, capability, last_success_at)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (layer, capability) DO UPDATE SET"
            "  blocked_until = NULL, last_success_at = EXCLUDED.last_success_at",
            (layer, capability, now),
        )
