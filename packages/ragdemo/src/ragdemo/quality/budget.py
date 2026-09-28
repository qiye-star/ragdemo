"""月度成本预算（docs/04-ingestion.md §5.3，总计划阶段 1 任务 1.6）。

账本是 `core.provider_snapshot.cost_cents`——每一次供应商调用在取数时都留了一行
（任务 1.1）：免费来源记 0，计费来源记实际费用，单价未配置的计费调用记 NULL。

- 本月（北京时间自然月）已用 ≥ 80%：告警（质量指标 `monthly_cost_ratio` 记为未通过）；
- ≥ 100%：`ensure_budget` 抛 `BudgetExceeded`，计费资产在发起下一次调用**之前**停下；
- 有 NULL 成本的调用：真实花费可能比账面高，单独计数并告警，但不当成免费、也不因为
  "不知道"就阻断整条管线——单价配置上之后自然转成可判断的数字。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg

from ragdemo.quality.metrics import MetricResult, record_metric

CST = timezone(timedelta(hours=8))
WARN_RATIO = Decimal("0.8")
METRIC = "monthly_cost_ratio"


class BudgetExceeded(RuntimeError):
    """本月已用预算达到上限：计费调用必须停下。"""


class BudgetLevel(enum.Enum):
    OK = "ok"
    WARN = "warn"  # ≥ 80%，还没到上限
    UNKNOWN = "unknown"  # 有单价未知的计费调用，账面数字不可信
    EXCEEDED = "exceeded"  # ≥ 100%


@dataclass(frozen=True)
class BudgetStatus:
    month_start: datetime
    spent_cny: Decimal
    cap_cny: Decimal
    unknown_cost_calls: int

    @property
    def ratio(self) -> Decimal:
        return self.spent_cny / self.cap_cny

    @property
    def level(self) -> BudgetLevel:
        if self.ratio >= 1:
            return BudgetLevel.EXCEEDED
        if self.ratio >= WARN_RATIO:
            return BudgetLevel.WARN
        if self.unknown_cost_calls:
            return BudgetLevel.UNKNOWN
        return BudgetLevel.OK

    def describe(self) -> str:
        text = (
            f"{self.month_start:%Y-%m} 已用 {self.spent_cny.normalize():f} 元 / 上限 "
            f"{self.cap_cny.normalize():f} 元（{self.ratio:.0%}）"
        )
        if self.unknown_cost_calls:
            text += f"；另有 {self.unknown_cost_calls} 次计费调用单价未知，实际花费可能更高"
        return text


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(CST)
    start = datetime(local.year, local.month, 1, tzinfo=CST)
    end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=CST)
    return start, end


def month_to_date(conn: psycopg.Connection, *, now: datetime, cap_cny: Decimal) -> BudgetStatus:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"now 必须带时区，收到 naive datetime: {now!r}")
    start, end = _month_bounds(now)
    row = conn.execute(
        "SELECT coalesce(sum(cost_cents), 0), count(*) FILTER (WHERE cost_cents IS NULL)"
        "  FROM core.provider_snapshot WHERE fetched_at >= %s AND fetched_at < %s",
        (start, end),
    ).fetchone()
    assert row is not None
    cents, unknown = row
    return BudgetStatus(start, Decimal(cents) / 100, cap_cny, int(unknown))


def ensure_budget(conn: psycopg.Connection, *, now: datetime, cap_cny: Decimal) -> BudgetStatus:
    """计费调用之前调用：超预算则抛 `BudgetExceeded`，否则返回当前状态。"""
    status = month_to_date(conn, now=now, cap_cny=cap_cny)
    if status.level is BudgetLevel.EXCEEDED:
        raise BudgetExceeded(
            f"月度预算已用尽，已停止计费调用：{status.describe()}。"
            "下月自动恢复；确需追加请调高 RAGDEMO_MONTHLY_BUDGET_CNY。"
        )
    return status


def record_budget_metric(
    conn: psycopg.Connection, status: BudgetStatus, *, partition: date
) -> None:
    """写一行 `monthly_cost_ratio` 到质量指标表：≥ 80% 即记为未通过（告警）。"""
    record_metric(
        conn,
        partition,
        MetricResult(
            METRIC,
            float(status.ratio),
            status.level is BudgetLevel.OK,  # 告警线、超限与"单价未知"都记为未通过
            threshold=float(WARN_RATIO),
            note=status.describe(),
        ),
    )
