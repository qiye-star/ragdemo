"""时点写入中间件。

三条不可妥协的规则（docs/03-point-in-time.md §3）：
1. 更正是追加，不是更新——给旧行打 superseded_at，再插新行；
2. superseded_at 等于新行的 known_at，不是 now()，这样任意 as_of 恰好命中一行；
3. known_at 来自数据本身（适配器计算），ingested_at 才是 now()。

数据库的 fin_fact_live_uk 部分唯一索引是最后一道防线：忘记第 1 步会直接冲突失败。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import cast

import psycopg

from ragdemo.adapters.base import FactRecord

_VALUE_EPSILON = 1e-9

Connection = psycopg.Connection[tuple[object, ...]]


class WriteOutcome(Enum):
    INSERTED = "inserted"
    SKIPPED_IDENTICAL = "skipped_identical"
    CORRECTED = "corrected"


class OutOfOrderCorrection(RuntimeError):
    """新数据的 known_at 早于当前有效行的 known_at。"""


class UnknownEntityRef(RuntimeError):
    """供应商代码无法解析为 entity_id。"""


class UnknownMetricField(RuntimeError):
    """供应商字段名无法映射为 metric_id。"""


class PointInTimeWriter:
    """把归一化记录写进时点表。"""

    def __init__(self, conn: Connection, *, ingest_run_id: str, source: str) -> None:
        self.conn = conn
        self.ingest_run_id = ingest_run_id
        self.source = source
        self._entity_cache: dict[str, str] = {}
        self._metric_cache: dict[str, str] = {}

    # --- 标识映射 ---------------------------------------------------------

    def _entity_id(self, entity_ref: str) -> str:
        if entity_ref not in self._entity_cache:
            row = self.conn.execute(
                "SELECT entity_id FROM core.entity WHERE tushare_code = %s"
                " OR ifind_code = %s OR wind_code = %s OR edgar_cik = %s",
                (entity_ref, entity_ref, entity_ref, entity_ref),
            ).fetchone()
            if row is None:
                raise UnknownEntityRef(f"无法解析供应商代码 {entity_ref!r} 为 entity_id")
            self._entity_cache[entity_ref] = str(row[0])
        return self._entity_cache[entity_ref]

    def _metric_id(self, metric_field: str) -> str:
        key = f"{self.source}:{metric_field}"
        if key not in self._metric_cache:
            row = self.conn.execute(
                "SELECT metric_id FROM core.metric_source_map "
                " WHERE provider = %s AND provider_field = %s ORDER BY priority LIMIT 1",
                (self.source, metric_field),
            ).fetchone()
            if row is None:
                raise UnknownMetricField(
                    f"{self.source} 的字段 {metric_field!r} 未在 metric_source_map 中登记"
                )
            self._metric_cache[key] = str(row[0])
        return self._metric_cache[key]

    # --- 写入 -------------------------------------------------------------

    def write_fact(self, record: FactRecord) -> WriteOutcome:
        """写一条事实。已存在相同值则跳过，值不同则走更正流程。"""
        entity_id = self._entity_id(record.entity_ref)
        metric_id = self._metric_id(record.metric_field)

        with self.conn.transaction():
            live = self.conn.execute(
                "SELECT fact_id, value, known_at FROM core.fin_fact "
                " WHERE entity_id = %s AND metric_id = %s AND period = %s"
                "   AND superseded_at IS NULL FOR UPDATE",
                (entity_id, metric_id, record.period),
            ).fetchone()

            if live is not None:
                # 行工厂固定为 tuple[object, ...]（见 Connection 别名），这里的窄化
                # 是已知的、受约束的：这两列在 schema 里就是 numeric / timestamptz。
                existing_value = cast(Decimal, live[1])
                existing_known_at = cast(datetime, live[2])
                if record.known_at < existing_known_at:
                    raise OutOfOrderCorrection(
                        f"{entity_id}/{metric_id}/{record.period}: 新数据 known_at "
                        f"{record.known_at} 早于现有 {existing_known_at}"
                    )
                if abs(float(existing_value) - record.value) < _VALUE_EPSILON:
                    return WriteOutcome.SKIPPED_IDENTICAL
                self.conn.execute(
                    "UPDATE core.fin_fact SET superseded_at = %s "
                    " WHERE entity_id = %s AND metric_id = %s AND period = %s"
                    "   AND superseded_at IS NULL",
                    (record.known_at, entity_id, metric_id, record.period),
                )
                outcome = WriteOutcome.CORRECTED
            else:
                outcome = WriteOutcome.INSERTED

            self.conn.execute(
                "INSERT INTO core.fin_fact (entity_id, metric_id, period, period_end,"
                " value, unit, currency, valid_from, known_at, source, source_ref,"
                " ingest_run_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    entity_id,
                    metric_id,
                    record.period,
                    record.period_end,
                    record.value,
                    record.unit,
                    record.currency,
                    record.valid_from,
                    record.known_at,
                    self.source,
                    record.source_ref,
                    self.ingest_run_id,
                ),
            )
        return outcome

    def write_facts(self, records: Iterable[FactRecord]) -> dict[WriteOutcome, int]:
        counts: dict[WriteOutcome, int] = {}
        for record in records:
            outcome = self.write_fact(record)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts
