"""质量指标写入（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 D）。

`record_metric` 是 `quality.quality_metric` 唯一的写入路径——`quality/checks.py`
的每个 `@asset_check` 算完一个数字都调它一次，通过与失败都写。只记失败看不出
"从 99% 滑到 96%" 这种退化趋势，自动指标的价值在趋势，不在单点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg


@dataclass(frozen=True)
class MetricResult:
    """一次指标计算的结果。`passed` 由调用方按自己的比较方向算好再传入——
    有的指标"越大越好"（成功率），有的"越小越好"（缺页率），`record_metric`
    不替调用方猜方向。"""

    metric: str
    value: float
    passed: bool
    threshold: float | None = None
    source_id: str | None = None
    note: str | None = None


def record_metric(
    conn: psycopg.Connection,
    partition_date: date,
    result: MetricResult,
) -> None:
    conn.execute(
        "INSERT INTO quality.quality_metric"
        " (metric, source_id, partition_date, value, threshold, passed, note)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            result.metric,
            result.source_id,
            partition_date,
            result.value,
            result.threshold,
            result.passed,
            result.note,
        ),
    )
    conn.commit()
