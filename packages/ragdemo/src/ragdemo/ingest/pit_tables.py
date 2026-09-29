"""股东 / 分部 / 汇率三张时点表共用的写入器（阶段二 Task 3）。

语义（一个"批"= 一个 (实体, 期别) 的全部行，汇率则是一条 (币种对, 日期)）：
- 没有现行行 → 全部插入，`known_at` = 调用方给的首次披露时刻，
  `known_at_basis = 'first_periodic_report'`；
- 有现行行且内容完全相同 → `UNCHANGED`，什么都不写；
- 内容不同 → 旧行 `superseded_at = observed_at`，新行 `known_at = observed_at`、
  `known_at_basis = 'revision_observed'`——不改写历史，更正之前的 `as_of` 仍看到旧值。
  `observed_at` 不晚于旧行 `known_at` 时抛 `OutOfOrderCorrection`。

整批比对：股东第 10 名消失、分部少了一项，同样算变化。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import psycopg

from ragdemo.adapters.structured_rows import FxRow, HoldingRow, SegmentRow
from ragdemo.ingest.writer import OutOfOrderCorrection


class Outcome(StrEnum):
    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    REVISED = "revised"


def _replace_batch(
    conn: psycopg.Connection,
    *,
    table: str,
    scope: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    compare: Sequence[str],
    common: Mapping[str, Any],
    known_at: datetime,
    observed_at: datetime,
    with_basis: bool,
) -> Outcome:
    where = " AND ".join(f"{k} = %s" for k in scope)
    live = conn.execute(
        f"SELECT known_at, {', '.join(compare)} FROM {table}"
        f" WHERE {where} AND superseded_at IS NULL",
        tuple(scope.values()),
    ).fetchall()
    new_set = {tuple(r[c] for c in compare) for r in rows}
    if live:
        if {tuple(r[1:]) for r in live} == new_set and len(live) == len(rows):
            return Outcome.UNCHANGED
        latest_known = max(r[0] for r in live)
        if observed_at <= latest_known:
            raise OutOfOrderCorrection(
                f"{table} {dict(scope)}：观察时刻 {observed_at} 不晚于现行 known_at {latest_known}"
            )
        conn.execute(
            f"UPDATE {table} SET superseded_at = %s WHERE {where} AND superseded_at IS NULL",
            (observed_at, *scope.values()),
        )
        effective, basis, outcome = observed_at, "revision_observed", Outcome.REVISED
    else:
        effective, basis, outcome = known_at, "first_periodic_report", Outcome.INSERTED

    for row in rows:
        values: dict[str, Any] = {**scope, **common, **row, "known_at": effective}
        if with_basis:
            values["known_at_basis"] = basis
        cols = ", ".join(values)
        marks = ", ".join(["%s"] * len(values))
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})",
            tuple(values.values()),
        )
    return outcome


def write_holdings(
    conn: psycopg.Connection,
    *,
    entity_id: str,
    period: str,
    period_end: date,
    rows: Sequence[HoldingRow],
    known_at: datetime,
    source: str,
    source_ref: str | None,
    snapshot_id: int,
    ingest_run_id: str,
    observed_at: datetime,
) -> Outcome:
    return _replace_batch(
        conn,
        table="core.shareholder_holding",
        scope={"entity_id": entity_id, "period": period},
        rows=[
            {
                "rank": r.rank,
                "holder_name": r.holder_name,
                "shares": r.shares,
                "ratio": r.ratio,
            }
            for r in rows
        ],
        compare=("rank", "holder_name", "shares", "ratio"),
        common={
            "period_end": period_end,
            "valid_from": period_end,
            "source": source,
            "source_ref": source_ref,
            "snapshot_id": snapshot_id,
            "ingest_run_id": ingest_run_id,
        },
        known_at=known_at,
        observed_at=observed_at,
        with_basis=True,
    )


def write_segments(
    conn: psycopg.Connection,
    *,
    entity_id: str,
    period: str,
    period_end: date,
    rows: Sequence[SegmentRow],
    currency: str,
    known_at: datetime,
    source: str,
    source_ref: str | None,
    snapshot_id: int,
    ingest_run_id: str,
    observed_at: datetime,
) -> Outcome:
    return _replace_batch(
        conn,
        table="core.segment_revenue",
        scope={"entity_id": entity_id, "period": period},
        rows=[
            {
                "dimension": r.dimension,
                "segment_name": r.name,
                "revenue": r.revenue,
                "gross_profit": r.gross_profit,
                "revenue_share": r.share,
            }
            for r in rows
        ],
        compare=("dimension", "segment_name", "revenue", "gross_profit", "revenue_share"),
        common={
            "period_end": period_end,
            "valid_from": period_end,
            "currency": currency,
            "source": source,
            "source_ref": source_ref,
            "snapshot_id": snapshot_id,
            "ingest_run_id": ingest_run_id,
        },
        known_at=known_at,
        observed_at=observed_at,
        with_basis=True,
    )


def write_fx(
    conn: psycopg.Connection,
    *,
    rows: Sequence[FxRow],
    source: str,
    snapshot_id: int,
    ingest_run_id: str,
    observed_at: datetime,
) -> dict[Outcome, int]:
    """逐条（币种对, 日期）单独比对；汇率行没有 `known_at_basis` 列。"""
    counts = dict.fromkeys(Outcome, 0)
    for r in rows:
        outcome = _replace_batch(
            conn,
            table="core.fx_rate",
            scope={"base_ccy": r.base_ccy, "quote_ccy": r.quote_ccy, "rate_date": r.rate_date},
            rows=[{"rate": r.rate}],
            compare=("rate",),
            common={
                "valid_from": r.rate_date,
                "source": source,
                "source_ref": f"{r.base_ccy}/{r.quote_ccy}:{r.rate_date.isoformat()}",
                "snapshot_id": snapshot_id,
                "ingest_run_id": ingest_run_id,
            },
            known_at=r.known_at,
            observed_at=observed_at,
            with_basis=False,
        )
        counts[outcome] += 1
    return counts
