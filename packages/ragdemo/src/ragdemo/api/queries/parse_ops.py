"""分档与预算查询：A/B/C 档路由、生效策略、月度预算、重试队列、告警。

`core.parse_tier_policy` / `core.parse_retry_queue` 都不在 `core.
bitemporal_registry` 里，没有 `asof` 视图（web-diagnostic-ui 计划裁决 2）。
这里刻意**复用** `ragdemo.parse.router` 里已经写好且已测的谓词/算法
（`load_active_policy` 的排序、`monthly_pages_spent` 的裸 sum、
`RetryQueueEntry.is_overdue` 的判定），而不是在这一层重新实现一遍——
两处口径一旦漂了，应该是接口层的测试红，不是界面自己悄悄给出不同的数。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import psycopg

from ragdemo.api.schemas import (
    BudgetBucket,
    BudgetResponse,
    ParseEngineBucket,
    PoliciesResponse,
    PolicyRow,
    RetryQueueResponse,
    RetryQueueRow,
    TierBucket,
    TierDistributionResponse,
    WarningAggregate,
    WarningDocument,
    WarningsResponse,
)
from ragdemo.api.serialize import (
    as_bool,
    as_datetime,
    as_int,
    as_optional_float,
    as_optional_str,
    as_str,
)
from ragdemo.config import ConfigError, load_config
from ragdemo.parse.router import RetryQueueEntry, monthly_pages_spent

TIER_RULE = (
    "failed = 该文档在 as_of 下零块（与 quality/checks.py::"
    "parse_success_rate_check 同一判定，优先于其余规则——一份解析失败的"
    "文档不该因为 parse_engine 字符串长得像成功而被误分类）；"
    "A = parse_engine 前缀 vendor（供应商结构化接口，不走 xParse）；"
    "B = 前缀 textin 且 supersedes_doc_id IS NULL；"
    "C = 前缀 textin 且 supersedes_doc_id IS NOT NULL（重解析新行）；"
    "other = 其余（NULL 或未知前缀）"
)

_TIER_ORDER = ("A", "B", "C", "failed", "other")


def classify_tier(
    *, parse_engine: str | None, supersedes_doc_id: int | None, block_count: int
) -> str:
    """纯函数，单测直接调用不需要数据库。"""
    if block_count == 0:
        return "failed"
    if parse_engine is None:
        return "other"
    if parse_engine.startswith("vendor:"):
        return "A"
    if parse_engine.startswith("textin:"):
        return "C" if supersedes_doc_id is not None else "B"
    return "other"


_TIER_SOURCE_SQL = """
    SELECT d.doc_id, d.parse_engine, d.supersedes_doc_id, d.parse_confidence,
           (d.parse_warnings @> '["budget_exceeded"]'::jsonb) AS budget_exceeded,
           (SELECT count(*) FROM asof.doc_block b WHERE b.doc_id = d.doc_id) AS block_count
      FROM asof.document d
     WHERE d.owner_tenant IS NULL AND d.owner_user IS NULL
"""


def get_tier_distribution(
    conn: psycopg.Connection[tuple[object, ...]], *, as_of: datetime
) -> TierDistributionResponse:
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        rows = conn.execute(_TIER_SOURCE_SQL).fetchall()

    by_tier: dict[str, list[tuple[float | None, bool]]] = {t: [] for t in _TIER_ORDER}
    by_engine: dict[str | None, int] = {}
    for r in rows:
        parse_engine = as_optional_str(r[1])
        supersedes_doc_id = as_int(r[2]) if r[2] is not None else None
        confidence = as_optional_float(r[3])
        budget_exceeded = as_bool(r[4])
        block_count = as_int(r[5])

        tier = classify_tier(
            parse_engine=parse_engine, supersedes_doc_id=supersedes_doc_id, block_count=block_count
        )
        by_tier.setdefault(tier, []).append((confidence, budget_exceeded))
        by_engine[parse_engine] = by_engine.get(parse_engine, 0) + 1

    tiers: list[TierBucket] = []
    for tier in _TIER_ORDER:
        entries = by_tier.get(tier, [])
        if not entries:
            continue
        confidences = [c for c, _ in entries if c is not None]
        tiers.append(
            TierBucket(
                tier=tier,
                documents=len(entries),
                budget_exceeded=sum(1 for _, exceeded in entries if exceeded),
                confidence_avg=(sum(confidences) / len(confidences)) if confidences else None,
                confidence_min=min(confidences) if confidences else None,
            )
        )

    by_parse_engine = [
        ParseEngineBucket(parse_engine=engine, documents=count)
        for engine, count in sorted(by_engine.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    ]

    return TierDistributionResponse(
        as_of=as_of.isoformat(), tier_rule=TIER_RULE, tiers=tiers, by_parse_engine=by_parse_engine
    )


_POLICIES_SQL = """
    SELECT policy_id, doc_type, confidence_below, closure_below, monthly_cap_cny,
           enabled, known_at, superseded_at
      FROM core.parse_tier_policy
     ORDER BY (doc_type IS NULL), known_at DESC
"""


def get_policies(
    conn: psycopg.Connection[tuple[object, ...]], *, as_of: datetime
) -> PoliciesResponse:
    """全量历史 + 每行按 `load_active_policy` 同一套谓词算出的
    `active_at_as_of` 标记，不单独区分"只给活跃策略一个端点"——
    历史本身就是审计轨迹，界面据此画出"策略何时变过"。
    """
    rows = conn.execute(_POLICIES_SQL).fetchall()
    policies: list[PolicyRow] = []
    for r in rows:
        known_at = as_datetime(r[6])
        superseded_at_raw = r[7]
        superseded_at = as_datetime(superseded_at_raw) if superseded_at_raw is not None else None
        active = known_at <= as_of and (superseded_at is None or superseded_at > as_of)
        policies.append(
            PolicyRow(
                policy_id=as_int(r[0]),
                doc_type=as_optional_str(r[1]),
                confidence_below=as_optional_float(r[2]),
                closure_below=as_optional_float(r[3]),
                monthly_cap_cny=str(r[4]),
                enabled=as_bool(r[5]),
                known_at=known_at.isoformat(),
                superseded_at=superseded_at.isoformat() if superseded_at else None,
                active_at_as_of=active,
            )
        )
    return PoliciesResponse(as_of=as_of.isoformat(), policies=policies)


_ACTIVE_CAP_SQL = """
    SELECT monthly_cap_cny FROM core.parse_tier_policy
     WHERE (doc_type = %(doc_type)s OR doc_type IS NULL)
       AND known_at <= %(as_of)s AND (superseded_at IS NULL OR superseded_at > %(as_of)s)
     ORDER BY (doc_type IS NULL), known_at DESC
     LIMIT 1
"""

_DISTINCT_DOC_TYPES_IN_WINDOW_SQL = """
    SELECT DISTINCT source_id FROM quality.quality_metric
     WHERE metric = 'c_tier_pages' AND computed_at <= %(as_of)s
       AND partition_date >= %(month_start)s AND partition_date < %(month_end)s
       AND source_id IS NOT NULL
     ORDER BY 1
"""

_BUCKET_DETAIL_SQL = """
    SELECT coalesce(sum(value), 0) AS pages, count(*) AS rows
      FROM quality.quality_metric
     WHERE metric = 'c_tier_pages' AND source_id = %(doc_type)s
       AND computed_at <= %(as_of)s
       AND partition_date >= %(month_start)s AND partition_date < %(month_end)s
"""


def _month_bounds(as_of: datetime, month: str | None) -> tuple[date, date]:
    if month is not None:
        year, mo = (int(p) for p in month.split("-", 1))
    else:
        year, mo = as_of.year, as_of.month
    start = date(year, mo, 1)
    end = date(year + 1, 1, 1) if mo == 12 else date(year, mo + 1, 1)
    return start, end


def get_budget(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    month: str | None = None,
) -> BudgetResponse:
    month_start, month_end = _month_bounds(as_of, month)

    try:
        cost_per_page = load_config().textin_cost_per_page_cny
    except ConfigError:
        cost_per_page = None

    doc_types = [
        as_str(r[0])
        for r in conn.execute(
            _DISTINCT_DOC_TYPES_IN_WINDOW_SQL,
            {"as_of": as_of, "month_start": month_start, "month_end": month_end},
        ).fetchall()
    ]

    buckets: list[BudgetBucket] = []
    for doc_type in doc_types:
        detail = conn.execute(
            _BUCKET_DETAIL_SQL,
            {
                "doc_type": doc_type,
                "as_of": as_of,
                "month_start": month_start,
                "month_end": month_end,
            },
        ).fetchone()
        assert detail is not None
        pages_spent = as_int(detail[0])
        rows_count = as_int(detail[1])

        # 与 monthly_pages_spent 逐字相同的谓词，充当口径校验的 oracle——
        # 两个数字必须永远相等（tests/api/test_ops_api.py 会断言这一点）。
        oracle = monthly_pages_spent(
            conn,
            doc_type=doc_type,
            month_start=datetime.combine(month_start, datetime.min.time()),
            month_end=datetime.combine(month_end, datetime.min.time()),
        )
        assert oracle == pages_spent, "budget 汇总与 monthly_pages_spent 口径不一致"

        cap_row = conn.execute(_ACTIVE_CAP_SQL, {"doc_type": doc_type, "as_of": as_of}).fetchone()
        cap_cny = str(cap_row[0]) if cap_row is not None else None

        spent_cny = str(Decimal(pages_spent) * cost_per_page) if cost_per_page is not None else None

        buckets.append(
            BudgetBucket(
                doc_type=doc_type,
                pages_spent=pages_spent,
                rows=rows_count,
                cap_cny=cap_cny,
                cost_per_page_configured=cost_per_page is not None,
                spent_cny=spent_cny,
            )
        )

    return BudgetResponse(
        as_of=as_of.isoformat(),
        month_start=month_start.isoformat(),
        month_end=month_end.isoformat(),
        buckets=buckets,
    )


_RETRY_QUEUE_SQL = """
    SELECT source, provider_doc_id, first_failed_at, retry_deadline, last_seen_at, attempts
      FROM core.parse_retry_queue
     WHERE first_failed_at <= %(as_of)s
       AND (%(source)s::text IS NULL OR source = %(source)s)
     ORDER BY first_failed_at
"""


def get_retry_queue(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    source: str | None = None,
) -> RetryQueueResponse:
    rows = conn.execute(_RETRY_QUEUE_SQL, {"as_of": as_of, "source": source}).fetchall()
    entries: list[RetryQueueRow] = []
    for r in rows:
        entry = RetryQueueEntry(
            provider_doc_id=as_str(r[1]),
            first_failed_at=as_datetime(r[2]),
            retry_deadline=as_datetime(r[3]),
            last_seen_at=as_datetime(r[4]),
            attempts=as_int(r[5]),
        )
        entries.append(
            RetryQueueRow(
                source=as_str(r[0]),
                provider_doc_id=entry.provider_doc_id,
                first_failed_at=entry.first_failed_at.isoformat(),
                retry_deadline=entry.retry_deadline.isoformat(),
                last_seen_at=entry.last_seen_at.isoformat(),
                attempts=entry.attempts,
                overdue=entry.is_overdue(as_of),
            )
        )
    return RetryQueueResponse(
        as_of=as_of.isoformat(),
        point_in_time="not_replayable",
        reason=(
            "attempts/last_seen_at 原地更新，成功入库后行被物理删除"
            "（router.py::resolve_retry_pending）；只能按 first_failed_at 过滤，"
            "无法还原历史时刻的队列状态"
        ),
        entries=entries,
    )


_WARNINGS_AGGREGATE_SQL = """
    SELECT w, count(*)
      FROM asof.document d, jsonb_array_elements_text(d.parse_warnings) AS w
     WHERE d.owner_tenant IS NULL AND d.owner_user IS NULL
     GROUP BY 1 ORDER BY 2 DESC, 1
"""

_WARNINGS_DOCUMENTS_SQL = """
    SELECT doc_id, doc_type, title, source, publish_at, page_count,
           parse_engine, parse_confidence
      FROM asof.document
     WHERE parse_warnings @> jsonb_build_array(%(warning)s::text)
       AND owner_tenant IS NULL AND owner_user IS NULL
     ORDER BY publish_at DESC LIMIT %(limit)s
"""


def get_warnings(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    warning: str | None = None,
    limit: int = 50,
) -> WarningsResponse:
    from ragdemo_core.db.session import as_of_session

    with as_of_session(conn, as_of):
        agg_rows = conn.execute(_WARNINGS_AGGREGATE_SQL).fetchall()
        doc_rows = (
            conn.execute(_WARNINGS_DOCUMENTS_SQL, {"warning": warning, "limit": limit}).fetchall()
            if warning is not None
            else []
        )

    return WarningsResponse(
        as_of=as_of.isoformat(),
        aggregate=[
            WarningAggregate(warning=as_str(r[0]), documents=as_int(r[1])) for r in agg_rows
        ],
        documents=[
            WarningDocument(
                doc_id=as_int(r[0]),
                doc_type=as_str(r[1]),
                title=as_str(r[2]),
                source=as_str(r[3]),
                publish_at=as_datetime(r[4]).isoformat(),
                page_count=as_int(r[5]) if r[5] is not None else None,
                parse_engine=as_optional_str(r[6]),
                parse_confidence=as_optional_float(r[7]),
            )
            for r in doc_rows
        ],
    )


__all__ = (
    "TIER_RULE",
    "classify_tier",
    "get_budget",
    "get_policies",
    "get_retry_queue",
    "get_tier_distribution",
    "get_warnings",
)
