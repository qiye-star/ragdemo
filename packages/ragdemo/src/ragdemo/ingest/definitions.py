"""Dagster Definitions 与传感器。

`new_document_sensor` 读 core.document，把未处理的新文档行（按 doc_id 游标）
封装成 RunRequest / SkipReason 返回，本身不写库。真正的衔接点（写 core.event
候选行，供 LangGraph 侧监听器按 event_id 取任务）留给 Agent 层接入时实现——
**Dagster 资产不直接调用 Agent**，避免数据管线被模型调用的延迟与失败拖垮
（docs/01-architecture.md §3）。
"""

from __future__ import annotations

import os

import psycopg
from dagster import (
    DefaultSensorStatus,
    Definitions,
    InitResourceContext,
    RunRequest,
    SensorEvaluationContext,
    SkipReason,
    define_asset_job,
    resource,
    sensor,
)

from ragdemo.adapters.mock.facts import MockFactAdapter
from ragdemo.ingest.assets import fact_normalized, fin_fact_loaded
from ragdemo.ingest.writer import PointInTimeWriter

TRACKED_DOC_TYPES = ("quarterly", "annual_report", "announcement", "10-K", "10-Q", "8-K")


def _conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["RAGDEMO_DSN"])


@resource
def writer_resource(_context: InitResourceContext) -> PointInTimeWriter:
    """惰性构造：只有 Dagster 真正初始化该资源时才会连库，而不是模块导入时。"""
    return PointInTimeWriter(_conn(), ingest_run_id="dagster", source="mock")


# 临时目标：传感器发现新文档后，重新物化这两个事实资产。
# 真正的行为（写 core.event 候选行）留给 Agent 层接入时实现——见模块 docstring。
_document_reaction_job = define_asset_job(
    "document_reaction_placeholder", selection=[fact_normalized, fin_fact_loaded]
)


@sensor(
    minimum_interval_seconds=300,
    default_status=DefaultSensorStatus.STOPPED,
    job=_document_reaction_job,
)
def new_document_sensor(ctx: SensorEvaluationContext) -> RunRequest | SkipReason:
    """新文档触发下游处理。

    临时目标：重新物化事实资产（`_document_reaction_job`）。真正的行为
    （写 core.event 候选行，判定条件见 docs/07-agents.md §7）留给 Agent 层
    接入时实现——当前只保证扫到新文档时能安全触发一次 run，不会因为
    传感器缺少 job 目标而在 evaluate_tick 时崩溃。
    """
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
    jobs=[_document_reaction_job],
    sensors=[new_document_sensor],
    resources={
        "adapter": MockFactAdapter(),
        "writer": writer_resource,
    },
)
