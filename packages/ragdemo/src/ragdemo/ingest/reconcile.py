"""对账与增量游标（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 F）。

对账的前提是「应到」有定义——单源在跑时做对账，做出来的是一张只有一行的表
（阶段 F 引言原话）。这里不去猜一个通用的"供应商应到条数"接口：不同来源的
"应到"含义完全不同（公告是这次窗口内供应商实际返回的文档数，行情是这次窗口
应有的交易日数），能算出"应到"的地方各自调 `reconcile_diff`，这个模块只提供
统一的记录与判定规则：**差异必须有归因**，没有归因就是 check 失败
（F1 验收）。

`ingest_watermark` 的读写也放在这里，而不是散在各适配器里——增量游标是运维
状态，不是任何一个数据源私有的概念，多个来源共用同一张 `core.ingest_watermark`
表。
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg

from ragdemo.quality.metrics import MetricResult, record_metric

RECONCILE_METRIC = "reconcile_diff"
INGEST_LATENCY_METRIC = "ingest_latency_p95_minutes"


def reconcile_counts(
    conn: psycopg.Connection,
    partition_date: date,
    source_id: str,
    *,
    expected: int,
    actual: int,
    note: str | None = None,
) -> MetricResult:
    """记一行「应到 vs 实到」的对账结果。

    `passed`：diff 为 0 时天然通过；diff 非 0 时必须有 `note`（差异归因）
    才算通过——这不是放宽标准，是把"允许有差异"的前提钉死成"必须说清楚
    为什么"（F1：`reconcile_diff ≠ 0 且 note IS NULL` 的行数必须为 0）。
    调用方不给归因就传 note=None，这里不会替它编一个。
    """
    diff = actual - expected
    passed = diff == 0 or note is not None
    result = MetricResult(
        RECONCILE_METRIC,
        float(diff),
        passed,
        threshold=0.0,
        source_id=source_id,
        note=note,
    )
    record_metric(conn, partition_date, result)
    return result


def compute_ingest_latency(
    conn: psycopg.Connection,
    partition_date: date,
    source_id: str,
    since: datetime,
    until: datetime,
    *,
    p95_threshold_minutes: float | None = None,
) -> MetricResult:
    """接入延迟：`ingested_at - publish_at` 的 P50/P95（分钟），按源分别记。

    `p95_threshold_minutes` 为 None 时只记录、不设阻断阈值——方案的
    「P95 < 15 分钟」对公告这类应该接近实时的源合理，对 EDGAR 这种日频源
    没有意义：一刀切会让这个指标对慢源永远红着，红灯被忽略之后就和没有
    监控一样（阶段 F 引言原话）。调用方按自己的源决定要不要传阈值，这里
    不替所有源猜同一个数字。

    这个窗口内一条记录都没有（`p95` 为 NULL）时不视为失败——没有新公告
    入库不代表接入变慢了，只代表这次分区没有可测量的样本。
    """
    row = conn.execute(
        "SELECT percentile_cont(0.5) WITHIN GROUP ("
        "        ORDER BY EXTRACT(EPOCH FROM (ingested_at - publish_at)) / 60.0),"
        "       percentile_cont(0.95) WITHIN GROUP ("
        "        ORDER BY EXTRACT(EPOCH FROM (ingested_at - publish_at)) / 60.0)"
        "  FROM core.document"
        " WHERE source = %s AND publish_at > %s AND publish_at <= %s",
        (source_id, since, until),
    ).fetchone()
    p50, p95 = row if row is not None else (None, None)
    passed = p95 is None or p95_threshold_minutes is None or float(p95) <= p95_threshold_minutes
    note = None if p50 is None else f"p50={float(p50):.2f}min"
    result = MetricResult(
        INGEST_LATENCY_METRIC,
        float(p95) if p95 is not None else -1.0,
        passed,
        threshold=p95_threshold_minutes,
        source_id=source_id,
        note=note,
    )
    record_metric(conn, partition_date, result)
    return result


def read_watermark(conn: psycopg.Connection, source_id: str, partition_date: date) -> str | None:
    """返回这个源截至 `partition_date`（含）为止最新推进到的游标。

    不是精确匹配 `partition_date` 自己的那一行——"从哪里继续"要看历史上
    最近一次成功推进到的位置，不管那次运行记在哪个 partition_date 名下。
    这也是 F2 那个"同一分区重跑，第二次拉取应显著更少"的验收场景成立的
    前提：第一次跑完把游标写成"这个 partition_date 已处理"，第二次跑
    读回的就是这同一个值，`TushareAdapter.fetch_daily` 据此判定这一天
    没有新区间可拉（见该方法 docstring）。
    """
    row = conn.execute(
        "SELECT cursor FROM core.ingest_watermark"
        " WHERE source_id = %s AND partition_date <= %s AND cursor IS NOT NULL"
        " ORDER BY partition_date DESC LIMIT 1",
        (source_id, partition_date),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def write_watermark(
    conn: psycopg.Connection, source_id: str, partition_date: date, cursor: str
) -> None:
    """upsert：同一 (source_id, partition_date) 重跑会推进同一行的游标，
    而不是各自留一行——`core.ingest_watermark` 的主键就是这两列。"""
    conn.execute(
        "INSERT INTO core.ingest_watermark (source_id, partition_date, cursor, updated_at)"
        " VALUES (%s, %s, %s, now())"
        " ON CONFLICT (source_id, partition_date)"
        " DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()",
        (source_id, partition_date, cursor),
    )
    conn.commit()


__all__ = (
    "INGEST_LATENCY_METRIC",
    "RECONCILE_METRIC",
    "compute_ingest_latency",
    "read_watermark",
    "reconcile_counts",
    "write_watermark",
)
