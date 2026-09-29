"""dry run：真正执行时最多会调用多少次 Wind（额度成本的上限估计）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from ragdemo.ingest.period_disclosure import first_disclosure
from ragdemo.ingest.structured_extras import StructuredEntity, _has_live_rows


@dataclass(frozen=True)
class CallPlan:
    would_call: int
    already_present: int
    skipped_no_disclosure: int


def plan_calls(
    conn: psycopg.Connection,
    *,
    table: str,
    entities: Sequence[StructuredEntity],
    periods: Sequence[str],
    refresh: bool = False,
) -> CallPlan:
    would = present = skipped = 0
    for ent in entities:
        for period in periods:
            if first_disclosure(conn, ent.entity_id, period) is None:
                skipped += 1
            elif not refresh and _has_live_rows(conn, table, ent.entity_id, period):
                present += 1
            else:
                would += 1
    return CallPlan(would, present, skipped)
