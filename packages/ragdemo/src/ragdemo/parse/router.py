"""C 档二次解析路由（docs/superpowers/plans/2026-09-22-data-foundation.md 阶段 G）。

触发条件依赖阶段 C 的 `parse_confidence`/`table_html`，"批量退化则暂停发布"
依赖阶段 D 的阻断能力——前置不齐就做这一步，只能做成一个没人看的开关
（阶段 G 引言原话）。这个模块只负责"要不要路由到 C 档"与"月度预算还够不够"
两件事，真正的重解析调用（走 `TextInParser` 换更高精度的参数）由调用方
（`ingest/assets_docs.py`）编排，路由器不持有解析器实例。

C 档触发规则（方案 §2.2 原文，按计划里的"冲突 3 裁决对换"）：
`doc_type ∈ 高价值集合` 或 `parse_confidence < confidence_below` 或
`块内含数值且表格闭合率 < closure_below`。"高价值集合"由
`core.parse_tier_policy.doc_type` 精确匹配表达（一条策略对应一个高价值
doc_type），不是在这里维护一份写死的集合列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

import psycopg

from ragdemo.quality.metrics import MetricResult, record_metric

C_TIER_PAGES_METRIC = "c_tier_pages"


@dataclass(frozen=True)
class ParseTierPolicy:
    policy_id: int
    doc_type: str | None
    confidence_below: float | None
    closure_below: float | None
    monthly_cap_cny: Decimal
    enabled: bool


def load_active_policy(
    conn: psycopg.Connection, doc_type: str, as_of: datetime
) -> ParseTierPolicy | None:
    """按 doc_type 精确匹配优先于通配（doc_type IS NULL），只看 as_of 时刻
    活着的行——与 asof schema 视图同一套「known_at <= as_of < superseded_at」
    判定，只是这张表不经过 asof 视图（它不在 bitemporal_registry 里）。"""
    row = conn.execute(
        "SELECT policy_id, doc_type, confidence_below, closure_below,"
        " monthly_cap_cny, enabled"
        "  FROM core.parse_tier_policy"
        " WHERE (doc_type = %s OR doc_type IS NULL)"
        "   AND known_at <= %s AND (superseded_at IS NULL OR superseded_at > %s)"
        " ORDER BY (doc_type IS NULL), known_at DESC"
        " LIMIT 1",
        (doc_type, as_of, as_of),
    ).fetchone()
    if row is None:
        return None
    return ParseTierPolicy(
        policy_id=int(row[0]),
        doc_type=cast("str | None", row[1]),
        confidence_below=float(row[2]) if row[2] is not None else None,
        closure_below=float(row[3]) if row[3] is not None else None,
        monthly_cap_cny=cast(Decimal, row[4]),
        enabled=bool(row[5]),
    )


def should_route_to_tier_c(
    policy: ParseTierPolicy | None,
    *,
    parse_confidence: float | None,
    table_closure_rate: float | None,
    has_numeric_table: bool,
) -> bool:
    """`policy` 为 None（没有匹配的策略）或策略被停用时，永远不路由——
    没有策略就没有触发规则，不能凭空猜一个默认阈值。"""
    if policy is None or not policy.enabled:
        return False
    if (
        policy.confidence_below is not None
        and parse_confidence is not None
        and parse_confidence < policy.confidence_below
    ):
        return True
    return bool(
        policy.closure_below is not None
        and has_numeric_table
        and table_closure_rate is not None
        and table_closure_rate < policy.closure_below
    )


@dataclass(frozen=True)
class BudgetDecision:
    """`allowed=False` 不代表调用方应该放弃这份文档的 C 档重解析——
    docs/superpowers 的 G3 验收明确要求超预算时仍然入库，只是标注低置信度
    并告警。这个字段只回答"预算允不允许现在花这笔钱"，花不花、花了怎么办
    是调用方的决定。"""

    allowed: bool
    spent_cny: Decimal
    cap_cny: Decimal


def record_c_tier_pages(
    conn: psycopg.Connection, *, doc_type: str, pages: int, at: datetime
) -> None:
    """记一次 C 档重解析消耗的页数——月度预算靠汇总这些行算出，不是另开
    一本"已花了多少钱"的流水账。"""
    record_metric(
        conn,
        at.date(),
        MetricResult(C_TIER_PAGES_METRIC, float(pages), True, source_id=doc_type),
    )


def monthly_pages_spent(
    conn: psycopg.Connection, *, doc_type: str, month_start: datetime, month_end: datetime
) -> int:
    """[month_start, month_end) 区间内，这个 doc_type 已经花掉的 C 档页数总和。

    按 `partition_date` 过滤，不按 `computed_at`——后者是这一行被写入的
    物理时刻（`quality.quality_metric.computed_at DEFAULT now()`），跟
    `core.document.ingested_at` 是同一类"入库时刻不代表业务时刻"的陷阱。
    `record_c_tier_pages` 的 `at` 参数才是这次重解析真正发生的时刻，落进
    `partition_date`；回填历史分区时 `computed_at` 永远是回填当天，用它算
    月度预算会把所有回填都算进回填当月，而不是历史数据实际所属的月份。
    """
    row = conn.execute(
        "SELECT coalesce(sum(value), 0) FROM quality.quality_metric"
        " WHERE metric = %s AND source_id = %s"
        "   AND partition_date >= %s AND partition_date < %s",
        (C_TIER_PAGES_METRIC, doc_type, month_start.date(), month_end.date()),
    ).fetchone()
    assert row is not None
    return int(row[0])


def check_monthly_budget(
    conn: psycopg.Connection,
    policy: ParseTierPolicy,
    *,
    doc_type: str,
    month_start: datetime,
    month_end: datetime,
    pages_about_to_spend: int,
    cost_per_page_cny: Decimal,
) -> BudgetDecision:
    """月度预算闸门。`cost_per_page_cny` 是必填参数，没有默认值——C 档的
    真实单价是商务定价，不该在代码里编一个数字，调用方从部署配置读（环境
    变量或类似机制），传不了就不该调用这个函数假装算出了一个准数。"""
    spent = Decimal(monthly_pages_spent(
        conn, doc_type=doc_type, month_start=month_start, month_end=month_end
    )) * cost_per_page_cny
    additional = Decimal(pages_about_to_spend) * cost_per_page_cny
    return BudgetDecision(
        allowed=(spent + additional) <= policy.monthly_cap_cny,
        spent_cny=spent,
        cap_cny=policy.monthly_cap_cny,
    )


# --- 重试队列（G6）------------------------------------------------------------

RETRY_WINDOW = timedelta(hours=24)


def record_retry_pending(
    conn: psycopg.Connection, *, source: str, provider_doc_ids: list[str], at: datetime
) -> None:
    """首次失败即入队；`first_failed_at`/`retry_deadline` 只在首次入队时
    固定，此后重复失败只推进 `last_seen_at`/`attempts`——与阶段 E 抽样重放
    "探测点只在首次抽中时固定"是同一个原则：这两个时间戳如果每次都重算，
    就再也说不清"这份文档到底卡了多久"。"""
    for provider_doc_id in provider_doc_ids:
        conn.execute(
            "INSERT INTO core.parse_retry_queue"
            " (source, provider_doc_id, first_failed_at, retry_deadline, last_seen_at, attempts)"
            " VALUES (%s, %s, %s, %s, %s, 1)"
            " ON CONFLICT (source, provider_doc_id) DO UPDATE"
            "   SET last_seen_at = EXCLUDED.last_seen_at,"
            "       attempts = core.parse_retry_queue.attempts + 1",
            (source, provider_doc_id, at, at + RETRY_WINDOW, at),
        )
    conn.commit()


def resolve_retry_pending(
    conn: psycopg.Connection, *, source: str, provider_doc_ids: list[str]
) -> None:
    """文档终于成功入库——从队列里摘除，不留一条"已解决"的历史行：
    这张表只回答"现在还卡着的有哪些"，不是审计日志。"""
    if not provider_doc_ids:
        return
    conn.execute(
        "DELETE FROM core.parse_retry_queue WHERE source = %s AND provider_doc_id = ANY(%s)",
        (source, provider_doc_ids),
    )
    conn.commit()


@dataclass(frozen=True)
class RetryQueueEntry:
    provider_doc_id: str
    first_failed_at: datetime
    retry_deadline: datetime
    last_seen_at: datetime
    attempts: int

    def is_overdue(self, as_of: datetime) -> bool:
        return as_of > self.retry_deadline


def list_retry_queue(conn: psycopg.Connection, *, source: str) -> list[RetryQueueEntry]:
    rows = conn.execute(
        "SELECT provider_doc_id, first_failed_at, retry_deadline, last_seen_at, attempts"
        "  FROM core.parse_retry_queue WHERE source = %s ORDER BY first_failed_at",
        (source,),
    ).fetchall()
    return [
        RetryQueueEntry(
            provider_doc_id=str(r[0]),
            first_failed_at=r[1],
            retry_deadline=r[2],
            last_seen_at=r[3],
            attempts=int(r[4]),
        )
        for r in rows
    ]


__all__ = (
    "C_TIER_PAGES_METRIC",
    "RETRY_WINDOW",
    "BudgetDecision",
    "ParseTierPolicy",
    "RetryQueueEntry",
    "check_monthly_budget",
    "list_retry_queue",
    "load_active_policy",
    "monthly_pages_spent",
    "record_c_tier_pages",
    "record_retry_pending",
    "resolve_retry_pending",
    "should_route_to_tier_c",
)
