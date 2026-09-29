"""股东 / 分部 / 汇率的入库编排（阶段二 Task 7）。

股东与分部：对每个 (实体, 期别)——
1. `first_disclosure`：公告目录里找不到该期定期报告 → 记入 `skipped_no_disclosure`、**不调 Wind**
   （没有可信 known_at 的数据不入库，也省额度）；
2. 库里已有该期现行行且没要求 `refresh` → 记入 `already_present`、不调 Wind
   （重跑不烧额度；要检测 Wind 追溯调整才 `refresh=True`）；
3. Wind 取数 → 先落 `provider_snapshot` 并提交（响应被拒也留下证据）→ 解析（被拒记入
   `rejected`，不中断整批）→ 分部再与 `fin_fact` 营业收入对账 → `pit_tables` 写入。

Wind 网关本身出问题（额度用尽、连接失败）连续 3 次就中止整批，不硬砸。汇率同理，
CFETS 的 WAF 一旦 403 就停。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.cfets import PAIRS, CfetsAdapter
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.wind import WindResponseRejected
from ragdemo.adapters.wind_structured import (
    WindHoldersAdapter,
    WindSegmentsAdapter,
    check_against_revenue,
)
from ragdemo.ingest.period_disclosure import first_disclosure, period_end_of
from ragdemo.ingest.pit_tables import Outcome, write_fx, write_holdings, write_segments
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo_core.blob import BlobStore
from ragdemo_core.db.session import as_of_session

MAX_CONSECUTIVE_GATEWAY_FAILURES = 3
HOLDER_KINDS = ("FY", "H1", "Q1", "Q3")
SEGMENT_KINDS = ("FY", "H1")
_PERIOD_ENDS = {"FY": (12, 31), "H1": (6, 30), "Q1": (3, 31), "Q3": (9, 30)}
# 公告目录从 2023-01-01 才有：更早的期别一定找不到披露记录，不必问。
EARLIEST_PERIOD_END = date(2022, 12, 31)


@dataclass(frozen=True)
class StructuredEntity:
    entity_id: str
    windcode: str
    name: str


@dataclass
class IngestSummary:
    inserted: int = 0
    unchanged: int = 0
    revised: int = 0
    already_present: int = 0
    wind_calls: int = 0
    skipped_no_disclosure: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    unreconciled_to_fin_fact: list[str] = field(default_factory=list)
    aborted: str | None = None

    def count(self, outcome: Outcome) -> None:
        if outcome is Outcome.INSERTED:
            self.inserted += 1
        elif outcome is Outcome.REVISED:
            self.revised += 1
        else:
            self.unchanged += 1


def a_share_entities(
    conn: psycopg.Connection, refs: Sequence[str] | None = None
) -> list[StructuredEntity]:
    rows = conn.execute(
        "SELECT entity_id, tushare_code, coalesce(name_short, name_full) FROM core.entity"
        " WHERE tushare_code IS NOT NULL AND market IN ('SSE', 'SZSE', 'BSE')"
        " ORDER BY tushare_code"
    ).fetchall()
    found = [StructuredEntity(str(r[0]), str(r[1]), str(r[2])) for r in rows]
    if refs:
        wanted = set(refs)
        found = [e for e in found if e.windcode in wanted]
    return found


def periods_back(today: date, count: int, kinds: Sequence[str]) -> list[str]:
    """最近 `count` 个已到期的期别（新→旧），如 ['2026Q2'…] 里按 kinds 过滤后的写法
    `2025H1` / `2024FY`。期末晚于 `today` 或早于 `EARLIEST_PERIOD_END` 的不含。"""
    out: list[str] = []
    for year in range(today.year, EARLIEST_PERIOD_END.year - 1, -1):
        for kind in sorted(kinds, key=lambda k: _PERIOD_ENDS[k], reverse=True):
            month, day = _PERIOD_ENDS[kind]
            end = date(year, month, day)
            if EARLIEST_PERIOD_END <= end <= today:
                out.append(f"{year}{kind}")
    return out[:count]


def _has_live_rows(conn: psycopg.Connection, table: str, entity_id: str, period: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE entity_id = %s AND period = %s"
        " AND superseded_at IS NULL LIMIT 1",
        (entity_id, period),
    ).fetchone()
    return row is not None


def fin_fact_revenue(
    conn: psycopg.Connection, entity_id: str, period: str, as_of: datetime
) -> Decimal | None:
    """`asof.fin_fact` 里该期的营业收入（元）。H1 在 fin_fact 里叫 `Q2`（累计口径）。"""
    year, kind = period[:4], period[4:]
    fin_period = f"{year}Q2" if kind == "H1" else period
    with as_of_session(conn, as_of):
        row = conn.execute(
            "SELECT value FROM asof.fin_fact WHERE entity_id = %s AND metric_id = 'revenue_total'"
            " AND period = %s",
            (entity_id, fin_period),
        ).fetchone()
    return Decimal(row[0]) if row else None


RevenueLookup = Callable[[psycopg.Connection, str, str, datetime], Decimal | None]


def ingest_holders(
    conn: psycopg.Connection,
    *,
    adapter: WindHoldersAdapter,
    blob: BlobStore,
    entities: Sequence[StructuredEntity],
    periods: Sequence[str],
    ingest_run_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    refresh: bool = False,
) -> IngestSummary:
    summary = IngestSummary()
    failures = 0
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=now().date())
    for ent in entities:
        for period in periods:
            key = f"{ent.windcode}/{period}"
            disc = first_disclosure(conn, ent.entity_id, period)
            if disc is None:
                summary.skipped_no_disclosure.append(key)
                continue
            if not refresh and _has_live_rows(
                conn, "core.shareholder_holding", ent.entity_id, period
            ):
                summary.already_present += 1
                continue
            try:
                summary.wind_calls += 1
                (raw,) = adapter.fetch(ctx, windcode=ent.windcode, name=ent.name, period=period)
                failures = 0
            except AdapterError as exc:
                failures += 1
                summary.rejected[key] = f"取数失败：{exc}"
                if failures >= MAX_CONSECUTIVE_GATEWAY_FAILURES:
                    summary.aborted = f"连续 {failures} 次 Wind 网关失败，已中止：{exc}"
                    return summary
                continue
            snapshot_id = record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
            conn.commit()  # 响应被拒也要留下原始证据
            try:
                parsed = adapter.parse(raw)
                outcome = write_holdings(
                    conn,
                    entity_id=ent.entity_id,
                    period=period,
                    period_end=period_end_of(period),
                    rows=parsed.rows,
                    known_at=disc.known_at,
                    source="wind",
                    source_ref=str(disc.index_id),
                    snapshot_id=snapshot_id,
                    ingest_run_id=ingest_run_id,
                    observed_at=now(),
                )
            except WindResponseRejected as exc:
                conn.rollback()
                summary.rejected[key] = str(exc)
                continue
            conn.commit()
            summary.count(outcome)
    return summary


def ingest_segments(
    conn: psycopg.Connection,
    *,
    adapter: WindSegmentsAdapter,
    blob: BlobStore,
    entities: Sequence[StructuredEntity],
    periods: Sequence[str],
    ingest_run_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    refresh: bool = False,
    revenue_lookup: RevenueLookup = fin_fact_revenue,
) -> IngestSummary:
    summary = IngestSummary()
    failures = 0
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=now().date())
    for ent in entities:
        for period in periods:
            key = f"{ent.windcode}/{period}"
            disc = first_disclosure(conn, ent.entity_id, period)
            if disc is None:
                summary.skipped_no_disclosure.append(key)
                continue
            if not refresh and _has_live_rows(conn, "core.segment_revenue", ent.entity_id, period):
                summary.already_present += 1
                continue
            try:
                summary.wind_calls += 1
                (raw,) = adapter.fetch(ctx, windcode=ent.windcode, name=ent.name, period=period)
                failures = 0
            except AdapterError as exc:
                failures += 1
                summary.rejected[key] = f"取数失败：{exc}"
                if failures >= MAX_CONSECUTIVE_GATEWAY_FAILURES:
                    summary.aborted = f"连续 {failures} 次 Wind 网关失败，已中止：{exc}"
                    return summary
                continue
            snapshot_id = record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
            conn.commit()
            try:
                rows = adapter.parse(raw)
                revenue = revenue_lookup(conn, ent.entity_id, period, now())
                if revenue is None:
                    summary.unreconciled_to_fin_fact.append(key)
                else:
                    check_against_revenue(rows, revenue)
                outcome = write_segments(
                    conn,
                    entity_id=ent.entity_id,
                    period=period,
                    period_end=period_end_of(period),
                    rows=rows,
                    currency="CNY",
                    known_at=disc.known_at,
                    source="wind",
                    source_ref=str(disc.index_id),
                    snapshot_id=snapshot_id,
                    ingest_run_id=ingest_run_id,
                    observed_at=now(),
                )
            except WindResponseRejected as exc:
                conn.rollback()
                summary.rejected[key] = str(exc)
                continue
            conn.commit()
            summary.count(outcome)
    return summary


def ingest_fx(
    conn: psycopg.Connection,
    *,
    adapter: CfetsAdapter,
    blob: BlobStore,
    start: date,
    end: date,
    ingest_run_id: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    pairs: Sequence[str] = PAIRS,
) -> IngestSummary:
    summary = IngestSummary()
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=end)
    for pair in pairs:
        try:
            for raw in adapter.fetch(ctx, pair=pair, start=start, end=end):
                snapshot_id = record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
                rows = adapter.parse(raw)
                counts = write_fx(
                    conn,
                    rows=rows,
                    source="cfets",
                    snapshot_id=snapshot_id,
                    ingest_run_id=ingest_run_id,
                    observed_at=now(),
                )
                conn.commit()
                summary.inserted += counts[Outcome.INSERTED]
                summary.unchanged += counts[Outcome.UNCHANGED]
                summary.revised += counts[Outcome.REVISED]
        except AdapterError as exc:
            conn.rollback()
            summary.rejected[pair] = str(exc)
            summary.aborted = f"{pair} 取数失败，已中止（已入库的部分保留，可重跑）：{exc}"
            return summary
    return summary
