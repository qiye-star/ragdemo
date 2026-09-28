"""每日记录一次本月已用预算比例的 Dagster 资产（总计划阶段 1 任务 1.6）。

单独成文件、且**不用** `from __future__ import annotations`：Dagster 在装饰期直接比较
`context` 参数的注解对象，延迟求值的字符串注解会让校验失败（`ingest/assets.py`
模块 docstring 记录过同一个坑）。预算的纯函数留在 `quality/budget.py`，CLI 用它时
不必背上 Dagster 依赖。
"""

from datetime import date, datetime

import psycopg
from dagster import AssetExecutionContext, ResourceParam, asset

from ragdemo.config import load_config
from ragdemo.ingest.assets import DAILY
from ragdemo.quality.budget import CST, BudgetLevel, month_to_date, record_budget_metric


@asset(partitions_def=DAILY, group_name="ops")
def monthly_cost(context: AssetExecutionContext, conn: ResourceParam[psycopg.Connection]) -> float:
    """本月（北京时间）已用预算比例。≥ 80% 记告警，≥ 100% 时计费资产自行阻断。"""
    status = month_to_date(conn, now=datetime.now(CST), cap_cny=load_config().monthly_budget_cny)
    record_budget_metric(conn, status, partition=date.fromisoformat(context.partition_key))
    conn.commit()
    if status.level is not BudgetLevel.OK:
        context.log.warning("monthly budget", extra={"status": status.describe()})
    return float(status.ratio)
