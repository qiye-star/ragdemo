"""待补清单 `core.ingest_backlog`（迁移 019，ADR-0012）。

主源不可用、又不允许跨源写入的数据（目前是财务）记在这里：同一 (capability, entity_ref)
只保留一条未解决项，`since` 取最早缺口；下次触发先补，补到覆盖 `since` 才算解决。

与 `PgSourceHealth` 一样要 autocommit 连接：入库失败回滚时，"这里还缺数据"这条记录
恰恰不能跟着消失。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from ragdemo.sources.failures import Failure
from ragdemo.sources.routing import Capability


@dataclass(frozen=True)
class BacklogItem:
    capability: str
    entity_ref: str
    since: date
    attempts: int
    failure_kind: str
    reason: str


class Backlog:
    def __init__(self, conn: psycopg.Connection) -> None:
        if not conn.autocommit:
            raise ValueError("Backlog 需要 autocommit 连接：待补记录不能随入库事务回滚")
        self._conn = conn

    def add(
        self,
        capability: Capability,
        entity_ref: str,
        *,
        since: date,
        failure: Failure,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO core.ingest_backlog (capability, entity_ref, since, failure_kind,"
            " reason, first_failed_at, last_failed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (capability, entity_ref) WHERE resolved_at IS NULL DO UPDATE SET"
            "  since = LEAST(core.ingest_backlog.since, EXCLUDED.since),"
            "  failure_kind = EXCLUDED.failure_kind, reason = EXCLUDED.reason,"
            "  attempts = core.ingest_backlog.attempts + 1,"
            "  last_failed_at = EXCLUDED.last_failed_at",
            (
                str(capability),
                entity_ref,
                since,
                failure.kind.value,
                failure.message,
                now,
                now,
            ),
        )

    def open_items(self, capability: Capability) -> list[BacklogItem]:
        rows = self._conn.execute(
            "SELECT capability, entity_ref, since, attempts, failure_kind, reason"
            " FROM core.ingest_backlog WHERE capability = %s AND resolved_at IS NULL"
            " ORDER BY since, entity_ref",
            (str(capability),),
        ).fetchall()
        return [
            BacklogItem(str(r[0]), str(r[1]), r[2], int(r[3]), str(r[4]), str(r[5])) for r in rows
        ]

    def resolve(
        self, capability: Capability, entity_ref: str, *, covered_since: date, now: datetime
    ) -> bool:
        """这次取数覆盖到了 `covered_since`：未解决项的 since 不早于它才算补上。"""
        cur = self._conn.execute(
            "UPDATE core.ingest_backlog SET resolved_at = %s"
            " WHERE capability = %s AND entity_ref = %s AND resolved_at IS NULL"
            "   AND since >= %s",
            (now, str(capability), entity_ref, covered_since),
        )
        return cur.rowcount > 0
