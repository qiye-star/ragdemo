"""Wind 财务事实入库：取数 → 原始快照 → 严格解析 → 时点写入（总计划阶段 2.1）。

逐实体处理：某个实体的响应被拒（结构 / 期别 / 单位不对，或供应商报错），只跳过这个
实体、记下原因，原始响应照样留在 provider_snapshot 里供排查；不会把半份解析结果写进库。

Wind 是财务唯一的写入源（ADR-0012）：不可用时不跨源补，而是把 (实体, 最早缺口) 记进
`core.ingest_backlog`；下次触发先把待补实体一并取上，请求区间往前扩到缺口，补上才解决。
第一次失败后 Wind 进入冷却（`core.source_health`），同一次运行里后面的实体直接进待补，
不再白打一次必然失败的请求。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import psycopg

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.wind import WindFundamentalsAdapter, quarter_ends_back
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.ingest.writer import (
    OutOfOrderCorrection,
    PointInTimeWriter,
    UnknownEntityRef,
    UnknownMetricField,
    WriteOutcome,
)
from ragdemo.sources.backlog import Backlog
from ragdemo.sources.failures import Failure, FailureKind, classify
from ragdemo.sources.health import SourceHealth
from ragdemo.sources.router import AllLayersFailed, Layer, SourceRouter
from ragdemo.sources.routing import ROUTES, Capability
from ragdemo_core.blob import BlobStore

CAPABILITY = Capability.FINANCIALS
(_LAYER,) = ROUTES[CAPABILITY]  # 分工表规定财务只有一层；多了这里会直接炸，提醒同步改


@dataclass
class IngestReport:
    written: int = 0  # 新插入 + 更正
    skipped: int = 0  # 与库中完全相同
    failed: dict[str, str] = field(default_factory=dict)  # windcode → 原因
    backlogged: dict[str, str] = field(default_factory=dict)  # windcode → 为什么进了待补
    warnings: list[str] = field(default_factory=list)  # 没入库的单条（披露日期不可信 / 无法核对）


def a_share_windcodes(conn: psycopg.Connection) -> list[str]:
    """Wind 代码与 Tushare 代码对 A 股相同（688041.SH / 000977.SZ）。海外实体不在这里。"""
    rows = conn.execute(
        "SELECT tushare_code FROM core.entity"
        " WHERE tushare_code IS NOT NULL AND market IN ('SSE', 'SZSE', 'BSE')"
        " ORDER BY tushare_code"
    ).fetchall()
    return [str(r[0]) for r in rows]


def ingest_wind_facts(
    conn: psycopg.Connection,
    adapter: WindFundamentalsAdapter,
    blob: BlobStore,
    *,
    windcodes: Sequence[str],
    partition_date: date,
    ingest_run_id: str,
    quarters: int,
    health: SourceHealth,
    backlog: Backlog | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IngestReport:
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=partition_date)
    writer = PointInTimeWriter(conn, ingest_run_id=ingest_run_id, source=_LAYER.source)
    router = SourceRouter(health, clock=clock)
    report = IngestReport()

    plan = {code: quarters for code in windcodes}
    for item in backlog.open_items(CAPABILITY) if backlog is not None else []:
        plan[item.entity_ref] = max(
            plan.get(item.entity_ref, 0), _quarters_since(item.since, partition_date)
        )

    for windcode, wanted in plan.items():
        ends = quarter_ends_back(partition_date, wanted)

        def fetch_one(code: str = windcode, n: int = wanted) -> list[tuple[RawResponse, int]]:
            fetched: list[tuple[RawResponse, int]] = []
            for raw in adapter.fetch(ctx, windcodes=[code], quarters=n):
                snapshot_id = record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
                fetched.append((raw, snapshot_id))
            return fetched

        try:
            routed = router.run(
                CAPABILITY, [Layer(_LAYER.name, _LAYER.source, _LAYER.degraded, fetch_one)]
            )
        except AllLayersFailed as exc:
            _to_backlog(backlog, report, windcode, ends[0], _failure_of(exc), clock())
            continue

        ((raw, snapshot_id),) = routed.value
        try:
            parsed = adapter.parse_with_warnings(raw)
            records = [replace(r, snapshot_id=snapshot_id) for r in parsed.records]
            outcomes = writer.write_facts(records)
        except (OutOfOrderCorrection, UnknownEntityRef, UnknownMetricField) as exc:
            conn.rollback()  # 我们这边的问题（口径映射 / 时序），要人看，不进待补
            report.failed[windcode] = str(exc)
            continue
        except AdapterError as exc:
            # 响应被拒：这个实体已写入的部分一并撤销；快照在解析前已提交，保留。
            # 数据确实缺着，进待补——Wind 的自然语言接口下次可能就答对了。
            conn.rollback()
            report.failed[windcode] = str(exc)
            _to_backlog(backlog, report, windcode, ends[0], classify(exc), clock())
            continue
        conn.commit()
        if backlog is not None:
            backlog.resolve(CAPABILITY, windcode, covered_since=ends[0], now=clock())
        report.warnings.extend(parsed.warnings)
        report.written += outcomes.get(WriteOutcome.INSERTED, 0)
        report.written += outcomes.get(WriteOutcome.CORRECTED, 0)
        report.skipped += outcomes.get(WriteOutcome.SKIPPED_IDENTICAL, 0)
    return report


def _quarters_since(since: date, partition_date: date) -> int:
    """从 `since` 所在季末到 `partition_date` 之前最近的季末，一共几个季度。"""
    n = 1
    while quarter_ends_back(partition_date, n)[0] > since:
        n += 1
    return n


def _failure_of(exc: AllLayersFailed) -> Failure:
    failures = [f for _, f in exc.attempts if f is not None]
    if failures:
        return failures[-1]
    return Failure(FailureKind.UNAVAILABLE, str(exc)[:500], timedelta(0))  # 只因冷却被跳过


def _to_backlog(
    backlog: Backlog | None,
    report: IngestReport,
    windcode: str,
    since: date,
    failure: Failure,
    now: datetime,
) -> None:
    report.backlogged[windcode] = f"{failure.kind.value}: {failure.message[:200]}"
    if backlog is not None:
        backlog.add(CAPABILITY, windcode, since=since, failure=failure, now=now)
