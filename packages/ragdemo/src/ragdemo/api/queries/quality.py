"""质量门禁看板查询：`quality.quality_metric` 是运维表，不在 `asof` schema
里（约束 5 对它不可满足，见 web-diagnostic-ui 计划裁决 2），但仍要用
`computed_at <= as_of` 做时点形状的过滤——`quality.dashboard` 视图本身
**没有**这道过滤，直接用它会让看板"看见未来"，所以这里自己重写
`DISTINCT ON` 而不是复用那个视图。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from ragdemo.api.schemas import DashboardResponse, MetricHistoryResponse, MetricPoint, MetricSpec
from ragdemo.api.serialize import (
    as_datetime,
    as_float,
    as_optional_float,
    as_optional_str,
    as_str,
)

SOURCE_TABLE = "quality.quality_metric"
AS_OF_FILTER = "computed_at <= as_of"
NON_ASOF_NOTE = "该表不在 asof schema，不是时点权威（见 db/migrations/011 表注释）"

# 静态注册表——**不要**靠 `SELECT DISTINCT metric` 反推，也不要从 Dagster
# 的 `AssetChecksDefinition` 反射读取：那需要把整条 Dagster 资源图（含
# TextInParser/MCP 网关等资源类）都 import 进这个本该"轻、能整体删除"的
# 诊断进程，成本和收益不成比例。这张表必须与
# `packages/ragdemo/src/ragdemo/quality/checks.py::ALL_CHECKS` 以及
# `ingest/reconcile.py`/`parse/router.py`/`cli.py` 里实际写入的 metric
# 名保持同步——`tests/api/test_sql_guards.py` 的
# `test_metric_registry_matches_source_metric_literals` 会扫这些模块的
# 源码文本（不 import，只读字符串字面量）做交叉核对。
METRIC_REGISTRY: tuple[MetricSpec, ...] = (
    MetricSpec(
        metric="parse_success_rate",
        is_gate=True,
        blocking=True,
        direction="higher_is_better",
        has_no_sample_sentinel=False,
        description="零块文档比例的补集——解析是否产出了内容",
    ),
    MetricSpec(
        metric="table_closure_rate",
        is_gate=True,
        blocking=True,
        direction="higher_is_better",
        has_no_sample_sentinel=False,
        description="表格闭合率",
    ),
    MetricSpec(
        metric="parse_confidence_p50",
        is_gate=True,
        blocking=False,
        direction="higher_is_better",
        has_no_sample_sentinel=True,
        description="块置信度中位数（无样本时 value=-1，passed 恒真）",
    ),
    MetricSpec(
        metric="orphan_block_count",
        is_gate=True,
        blocking=True,
        direction="lower_is_better",
        has_no_sample_sentinel=False,
        description="孤儿块数（应为 0）",
    ),
    MetricSpec(
        metric="leaf_length_compliance_rate",
        is_gate=True,
        blocking=False,
        direction="higher_is_better",
        has_no_sample_sentinel=False,
        description="叶子段落长度落在 [200,400] 的比例——只对 paragraph 叶子成立",
    ),
    MetricSpec(
        metric="vector_coverage_rate",
        is_gate=True,
        blocking=True,
        direction="higher_is_better",
        has_no_sample_sentinel=False,
        description="叶子块嵌入覆盖率",
    ),
    MetricSpec(
        metric="reconcile_diff",
        is_gate=True,
        blocking=True,
        direction=None,
        has_no_sample_sentinel=False,
        description="应到/实到对账差值——passed 由「diff=0 或已有归因 note」判定，"
        "不是单纯的阈值方向",
    ),
    MetricSpec(
        metric="ingest_latency_p95_minutes",
        is_gate=True,
        blocking=False,
        direction="lower_is_better",
        has_no_sample_sentinel=True,
        description="接入延迟 P95 分钟数（无样本时 value=-1，passed 恒真）",
    ),
    MetricSpec(
        metric="fetched_count",
        is_gate=False,
        blocking=None,
        direction=None,
        has_no_sample_sentinel=False,
        description="纯观测：这次窗口抓到的文档数，passed 恒真",
    ),
    MetricSpec(
        metric="c_tier_pages",
        is_gate=False,
        blocking=None,
        direction=None,
        has_no_sample_sentinel=False,
        description="C 档重解析消耗页数流水，供月度预算汇总",
    ),
    MetricSpec(
        metric="asof_replay_mismatch_count",
        is_gate=False,
        blocking=None,
        direction="lower_is_better",
        has_no_sample_sentinel=False,
        description="时点重放抽样发现的不一致数（cli.py，非 Dagster asset check）",
    ),
)

_DASHBOARD_SQL = """
    SELECT DISTINCT ON (metric, source_id)
           metric, source_id, partition_date, value::float8 AS value,
           threshold::float8 AS threshold, passed, note, computed_at
      FROM quality.quality_metric
     WHERE computed_at <= %(as_of)s
       AND (%(partition_date)s::date IS NULL OR partition_date = %(partition_date)s::date)
     ORDER BY metric, source_id, partition_date DESC, computed_at DESC
"""

_HISTORY_SQL = """
    SELECT DISTINCT ON (partition_date, source_id)
           source_id, partition_date, value::float8 AS value,
           threshold::float8 AS threshold, passed, note, computed_at
      FROM quality.quality_metric
     WHERE metric = %(metric)s
       AND computed_at <= %(as_of)s
       AND partition_date >= %(since)s
       AND (%(source_id)s::text IS NULL OR source_id = %(source_id)s)
     ORDER BY partition_date DESC, source_id, computed_at DESC
"""


def _row_to_point(row: tuple[object, ...], *, has_metric_col: bool) -> MetricPoint:
    """`_DASHBOARD_SQL` 多带一列 metric（第 0 列）——dashboard 汇总多个
    metric 在同一个列表里，前端按 metric 分组必须知道每一行是谁的；
    `_HISTORY_SQL` 已经用 `WHERE metric = %(metric)s` 固定了指标名，
    调用方从 URL 就知道，不需要每行重复一遍。
    """
    offset = 1 if has_metric_col else 0
    return MetricPoint(
        metric=as_str(row[0]) if has_metric_col else None,
        source_id=as_optional_str(row[offset]),
        partition_date=str(row[offset + 1]),
        value=as_float(row[offset + 2]),
        threshold=as_optional_float(row[offset + 3]),
        passed=bool(row[offset + 4]),
        note=as_optional_str(row[offset + 5]),
        computed_at=as_datetime(row[offset + 6]).isoformat(),
    )


def get_dashboard(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    partition_date: str | None = None,
) -> DashboardResponse:
    rows = conn.execute(
        _DASHBOARD_SQL, {"as_of": as_of, "partition_date": partition_date}
    ).fetchall()
    return DashboardResponse(
        as_of=as_of.isoformat(),
        source_table=SOURCE_TABLE,
        as_of_filter=AS_OF_FILTER,
        note=NON_ASOF_NOTE,
        metrics=[_row_to_point(r, has_metric_col=True) for r in rows],
    )


def get_metric_history(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    metric: str,
    days: int = 30,
    source_id: str | None = None,
) -> MetricHistoryResponse:
    since = (as_of - timedelta(days=days)).date()
    rows = conn.execute(
        _HISTORY_SQL,
        {"metric": metric, "as_of": as_of, "since": since, "source_id": source_id},
    ).fetchall()
    return MetricHistoryResponse(
        as_of=as_of.isoformat(),
        metric=metric,
        source_id=source_id,
        points=[_row_to_point(r, has_metric_col=False) for r in rows],
    )


__all__ = ("METRIC_REGISTRY", "get_dashboard", "get_metric_history")
