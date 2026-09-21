"""Mock 事实适配器。

默认数据取自真实三季报的公开数字，确保 P1 的评测不是在理想化数据上跑出来的。
known_at 一律由 partition_date 推导，因此回填历史分区时天然落在历史区间。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from ragdemo.adapters.base import FactRecord, FetchContext, RawResponse

_DEFAULT_ROWS: tuple[dict[str, Any], ...] = (
    {"ts_code": "688256.SH", "field": "revenue_total", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 12340.5, "unit": "CNY"},
    {"ts_code": "688256.SH", "field": "rd_expense", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 1890.2, "unit": "CNY"},
    {"ts_code": "002049.SZ", "field": "revenue_total", "period": "2024Q3",
     "period_end": "2024-09-30", "value": 4021.8, "unit": "CNY"},
)


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
        yield RawResponse(
            provider=self.provider,
            endpoint="/mock/facts",
            params={"partition": ctx.partition_date.isoformat(), **params},
            payload=[dict(r, ann_date=ctx.partition_date.isoformat()) for r in self._rows],
            http_status=200,
            fetched_at=datetime.now(UTC),
        )

    def known_at(self, record: Any) -> datetime:  # noqa: ANN401
        """公告日当日 23:59:59 —— 只给日期不给时间时的保守取法。"""
        ann = date.fromisoformat(str(record["ann_date"]))
        return datetime.combine(ann, time(23, 59, 59), tzinfo=UTC)

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
