"""Dagster Definitions 与传感器。

衔接点是 core.event 表：Dagster 入库新文档后写 event 候选行，
LangGraph 侧的监听器按 event_id 取任务。**Dagster 资产不直接调用 Agent**——
避免数据管线被模型调用的延迟与失败拖垮（docs/01-architecture.md §3）。
"""

from __future__ import annotations

import os

import psycopg
from dagster import (
    DefaultSensorStatus,
    Definitions,
    RunRequest,
    SensorEvaluationContext,
    SkipReason,
    sensor,
)

from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.ingest.assets import fact_normalized, fin_fact_loaded
from ragdemo.ingest.writer import PointInTimeWriter

TRACKED_DOC_TYPES = ("quarterly", "annual_report", "announcement", "10-K", "10-Q", "8-K")


def _conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["RAGDEMO_DSN"])


@sensor(minimum_interval_seconds=300, default_status=DefaultSensorStatus.STOPPED)
def new_document_sensor(ctx: SensorEvaluationContext) -> RunRequest | SkipReason:
    """新文档入库后写 event 候选行。判定条件见 docs/07-agents.md §7。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT doc_id FROM core.document "
            " WHERE doc_type = ANY(%s) AND superseded_at IS NULL"
            "   AND doc_id > COALESCE(%s, 0) ORDER BY doc_id LIMIT 50",
            (list(TRACKED_DOC_TYPES), ctx.cursor),
        ).fetchall()
    if not rows:
        return SkipReason("无新文档")
    ctx.update_cursor(str(rows[-1][0]))
    return RunRequest(run_key=f"docs-{rows[-1][0]}", run_config={})


defs = Definitions(
    assets=[fact_normalized, fin_fact_loaded],
    sensors=[new_document_sensor],
    resources={
        "adapter": MockFactAdapter(),
        "writer": PointInTimeWriter(_conn(), ingest_run_id="dagster", source="mock"),
    },
)
