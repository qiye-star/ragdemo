"""Mock 事实适配器。

默认数据取自真实三季报的公开数字，确保 P1 的评测不是在理想化数据上跑出来的。
known_at 一律由 partition_date 推导，因此回填历史分区时天然落在历史区间。

period_end 同样由 partition_date 推导（不再是硬编码的 2024-09-30）：真实世界里
一份定期报告总是滞后于它描述的期间结束——用固定天数近似这个报告滞后，
使得任意 partition_date（无论 2022 年还是 2024 年）产出的记录都满足
FactRecord 的不变量 known_at.date() >= period_end，而不是靠这个 Mock
去违反 core.fin_fact 的 known_at_before_period_end 泄漏自检
（ragdemo_core/db/invariants.py）。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse

CST = timezone(timedelta(hours=8))

# 「公告滞后」：真实财报从期间结束到公告披露一般要 30~45 天。用固定值把
# period_end 定在 partition_date 之前的这个窗口内，这样任意 partition_date
# 算出来的 known_at（= partition_date 当日 23:59:59）天然 >= period_end。
_REPORT_LAG_DAYS = 45

_DEFAULT_ROWS: tuple[dict[str, Any], ...] = (
    {"ts_code": "688256.SH", "field": "revenue_total", "value": 12340.5, "unit": "CNY"},
    {"ts_code": "688256.SH", "field": "rd_expense", "value": 1890.2, "unit": "CNY"},
    {"ts_code": "002049.SZ", "field": "revenue_total", "value": 4021.8, "unit": "CNY"},
)


def _period_end_for(partition_date: date) -> date:
    """把 partition_date 往前推 _REPORT_LAG_DAYS 天，作为「最近已结束的报告期」。"""
    return partition_date - timedelta(days=_REPORT_LAG_DAYS)


def _period_label_for(period_end: date) -> str:
    """从 period_end 推一个 YYYYQn 标签。这是 Mock，不追求精确对齐财季边界。"""
    quarter = (period_end.month - 1) // 3 + 1
    return f"{period_end.year}Q{quarter}"


class MockFactAdapter:
    """可注入记录的事实适配器。"""

    def __init__(
        self,
        records: Sequence[dict[str, Any]] | None = None,
        *,
        provider: str = "mock",
    ) -> None:
        self.provider = provider
        self._rows = tuple(records) if records is not None else _DEFAULT_ROWS
        self.call_count = 0

    def health(self) -> bool:
        return True

    def fetch(self, ctx: FetchContext, **params: Any) -> Iterator[RawResponse]:  # noqa: ANN401
        self.call_count += 1
        period_end = _period_end_for(ctx.partition_date)
        period = _period_label_for(period_end)
        yield RawResponse(
            provider=self.provider,
            endpoint="/mock/facts",
            params={"partition": ctx.partition_date.isoformat(), **params},
            payload=[
                dict(
                    r,
                    ann_date=ctx.partition_date.isoformat(),
                    period=period,
                    period_end=period_end.isoformat(),
                )
                for r in self._rows
            ],
            http_status=200,
            fetched_at=datetime.now(UTC),
        )

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        """公告日当日 23:59:59 本地时区（CST，UTC+8）—— 只给日期不给时间时的保守取法。"""
        ann = date.fromisoformat(str(record["ann_date"]))
        return datetime.combine(ann, time(23, 59, 59), tzinfo=CST)

    def parse(self, raw: RawResponse) -> Iterator[FactRecord]:
        for row in raw.payload:
            period_end = date.fromisoformat(str(row["period_end"]))
            yield FactRecord(
                entity_ref=str(row["ts_code"]),
                metric_field=str(row["field"]),
                period=str(row["period"]),
                period_end=period_end,
                value=float(row["value"]),
                unit=str(row["unit"]),
                currency="CNY",
                valid_from=period_end - timedelta(days=89),
                known_at=self.known_at(row),
                source_ref=f"{row['ts_code']}:{row['period']}:{row['field']}",
            )
