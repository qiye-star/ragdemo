"""把当天没通过的质量指标与预算状态转成告警（阶段 4.3）。

Dagster 的资产检查失败只在运行页面里可见；无人值守时需要主动推送。这里每天跑一次
（`daily_ops` 作业），读 `quality.quality_metric` 里这个分区日**未通过**的指标、再看
月度预算，各产生一条告警：
- 对账 / 新鲜度类指标（名字含 `reconcil`、`freshness`）→ `reconcile` 类，critical；
- 其它质量指标 → `quality` 类，warning（叶子长度合规这类只影响检索信号质量）；
- 预算 ≥ 80% → warning，≥ 100% → critical（计费调用已经被拒绝）。

告警键带分区日 / 月份：同一天同一指标只推一次，次日仍不达标会再提醒一次。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import psycopg

from ragdemo.ops.alerts import Alert
from ragdemo.quality.budget import BudgetLevel, month_to_date


def evaluate_quality_alerts(
    conn: psycopg.Connection, partition_date: date, *, cap_cny: Decimal, now: datetime
) -> list[Alert]:
    alerts: list[Alert] = []
    rows = conn.execute(
        "SELECT metric, source_id, value, threshold, note FROM quality.quality_metric"
        " WHERE partition_date = %s AND NOT passed ORDER BY metric, source_id NULLS FIRST",
        (partition_date,),
    ).fetchall()
    for metric, source_id, value, threshold, note in rows:
        name = str(metric)
        critical = "reconcil" in name or "freshness" in name
        scope = f"（来源 {source_id}）" if source_id else ""
        limit = f"，阈值 {threshold}" if threshold is not None else ""
        alerts.append(
            Alert(
                kind="reconcile" if critical else "quality",
                severity="critical" if critical else "warning",
                key=f"quality:{name}:{source_id or '-'}:{partition_date.isoformat()}",
                title=f"{partition_date.isoformat()} 质量指标未通过：{name}{scope}",
                detail=f"值 {value}{limit}" + (f"；{note}" if note else ""),
            )
        )

    status = month_to_date(conn, now=now, cap_cny=cap_cny)
    if status.level in (BudgetLevel.WARN, BudgetLevel.EXCEEDED):
        exceeded = status.level is BudgetLevel.EXCEEDED
        alerts.append(
            Alert(
                kind="budget",
                severity="critical" if exceeded else "warning",
                key=f"budget:{status.month_start:%Y-%m}:{status.level.value}",
                title="月度预算已用尽，计费调用已停止" if exceeded else "月度预算已用 80% 以上",
                detail=status.describe(),
            )
        )
    elif status.level is BudgetLevel.UNKNOWN:
        alerts.append(
            Alert(
                kind="budget",
                severity="warning",
                key=f"budget:{status.month_start:%Y-%m}:unknown",
                title="有计费调用单价未知，预算账面不可信",
                detail=status.describe(),
            )
        )
    return alerts
