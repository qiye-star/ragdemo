"""行情区间回填（阶段 2.3）：一次性补齐 [since, until] 的日行情。

复用 `PriceSources.route_daily`（Tushare MCP → Tushare REST → Wind 日 K 线三层降级，
每层都留快照），只是把取数区间改成一段：传给各层的 `FetchContext.watermark` 设为
`since` 的前一天（游标语义 = 已确认处理到哪天，含），`partition_date = until`，
于是各层用一次区间调用取回 [since, until]。

**不碰全局游标**：Dagster 的 `price_daily_loaded` 用一个全源共用的 `tushare` 游标；
这里只回填部分实体或部分区间时若推进它，会让 Dagster 之后的增量漏掉其余实体的缺口。
写入是幂等的（`PointInTimeWriter.write_prices`：相同收盘价跳过、不同则追加更正），
所以 Dagster 之后按自己的游标重拉重叠区间不会产生重复行。

任何一个实体所有层都不可用时**不中断整批**：记入 `failed`，其余实体照常入库
（与 Dagster 资产"整个分区失败"不同——一次性回填要的是尽量多补，失败的下次单独重跑）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.ingest.prices import PriceSources
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome
from ragdemo.sources.router import AllLayersFailed
from ragdemo_core.blob import BlobStore


@dataclass
class PriceBackfillReport:
    entities: int = 0
    inserted: int = 0
    corrected: int = 0
    skipped: int = 0
    by_layer: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def tushare_codes(conn: psycopg.Connection, refs: Sequence[str] | None = None) -> list[str]:
    rows = conn.execute(
        "SELECT tushare_code FROM core.entity WHERE tushare_code IS NOT NULL ORDER BY tushare_code"
    ).fetchall()
    codes = [str(r[0]) for r in rows]
    if refs:
        wanted = set(refs)
        codes = [c for c in codes if c in wanted]
    return codes


def backfill_prices(
    conn: psycopg.Connection,
    sources: PriceSources,
    blob: BlobStore,
    *,
    ts_codes: Sequence[str],
    since: date,
    until: date,
    ingest_run_id: str,
    on_progress: Callable[[str], None] | None = None,
) -> PriceBackfillReport:
    if since > until:
        raise ValueError(f"since {since} 晚于 until {until}")
    ctx = FetchContext(
        ingest_run_id=ingest_run_id,
        partition_date=until,
        watermark=(since - timedelta(days=1)).strftime("%Y%m%d"),
    )
    writer = PointInTimeWriter(conn, ingest_run_id=ingest_run_id, source="tushare")
    report = PriceBackfillReport()
    for ts_code in ts_codes:
        try:
            routed = sources.route_daily(ctx, conn, blob, ts_code)
        except AllLayersFailed as exc:
            conn.rollback()
            report.failed[ts_code] = str(exc)[:300]
            continue
        counts = writer.write_prices(routed.value)
        conn.commit()
        report.entities += 1
        report.by_layer[routed.layer] = report.by_layer.get(routed.layer, 0) + 1
        report.inserted += counts.get(WriteOutcome.INSERTED, 0)
        report.corrected += counts.get(WriteOutcome.CORRECTED, 0)
        report.skipped += counts.get(WriteOutcome.SKIPPED_IDENTICAL, 0)
        if on_progress:
            on_progress(f"{ts_code}: {len(routed.value)} 行（{routed.layer}）")
    return report
