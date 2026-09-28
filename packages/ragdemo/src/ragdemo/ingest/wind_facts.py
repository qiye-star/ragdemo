"""Wind 财务事实入库：取数 → 原始快照 → 严格解析 → 时点写入（总计划阶段 2.1）。

逐实体处理：某个实体的响应被拒（结构 / 期别 / 单位不对，或供应商报错），只跳过这个
实体、记下原因，原始响应照样留在 provider_snapshot 里供排查；不会把半份解析结果写进库。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.wind import PROVIDER, WindFundamentalsAdapter
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.ingest.writer import (
    OutOfOrderCorrection,
    PointInTimeWriter,
    UnknownEntityRef,
    UnknownMetricField,
    WriteOutcome,
)
from ragdemo_core.blob import BlobStore


@dataclass
class IngestReport:
    written: int = 0  # 新插入 + 更正
    skipped: int = 0  # 与库里完全相同
    failed: dict[str, str] = field(default_factory=dict)  # windcode → 原因
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
    partition_date: date,
    ingest_run_id: str,
    quarters: int | None = None,
) -> IngestReport:
    ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=partition_date)
    params = {} if quarters is None else {"quarters": quarters}
    writer = PointInTimeWriter(conn, ingest_run_id=ingest_run_id, source=PROVIDER)
    report = IngestReport()
    for raw in adapter.fetch(ctx, **params):
        windcode = str(raw.params["windcode"])
        snapshot_id = record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
        try:
            parsed = adapter.parse_with_warnings(raw)
            records = [replace(r, snapshot_id=snapshot_id) for r in parsed.records]
            outcomes = writer.write_facts(records)
        except (AdapterError, OutOfOrderCorrection, UnknownEntityRef, UnknownMetricField) as exc:
            conn.rollback()  # 这个实体已写入的部分一并撤销；快照在解析前已提交，保留
            report.failed[windcode] = str(exc)
            continue
        conn.commit()
        report.warnings.extend(parsed.warnings)
        report.written += outcomes.get(WriteOutcome.INSERTED, 0)
        report.written += outcomes.get(WriteOutcome.CORRECTED, 0)
        report.skipped += outcomes.get(WriteOutcome.SKIPPED_IDENTICAL, 0)
    return report
