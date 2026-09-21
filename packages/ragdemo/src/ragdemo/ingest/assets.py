"""Dagster 资产与日分区。

两条不可妥协的规则：
1. 资产必须幂等——同一分区重跑结果一致。实现靠时点写入中间件的 SKIPPED_IDENTICAL，
   不用 ON CONFLICT DO UPDATE（那会破坏时点语义）。
2. 回填历史分区时 known_at 必须是历史时刻。适配器的 known_at() 从记录本身计算，
   因此回填天然正确——这是把该逻辑放在适配器而非中间件的理由。

本文件刻意不用 `from __future__ import annotations`：Dagster 在 @asset 装饰期做
`context` 参数的类型校验时直接比较 `Parameter.annotation`（不解析延迟求值的字符串
注解），PEP 563 开着会导致该校验失败并抛出误导性的 DagsterInvalidDefinitionError。
Python 3.11 原生支持 `list[X]` / `X | None`，去掉这行不影响其余注解写法。

`adapter` / `writer` 用 `ResourceParam[...]` 包一层：`FactAdapter`（Protocol）与
`PointInTimeWriter`（普通类）都不是 Dagster 的 ResourceDefinition/ConfigurableResource
子类，不加这层标记 Dagster 会把它们当成需要上游资产产出的「输入」而不是
`Definitions(resources=...)` 注入的资源，`definitions.py` 里的装配会校验失败。
"""

from datetime import date

from dagster import AssetExecutionContext, DailyPartitionsDefinition, ResourceParam, asset

from ragdemo.adapters.base import FactAdapter, FactRecord, FetchContext
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome

DAILY = DailyPartitionsDefinition(start_date="2022-01-01", timezone="Asia/Shanghai")


def _run_id(context: AssetExecutionContext) -> str:
    # AssetExecutionContext.run_id 已弃用（改用 context.run.run_id），但直接调用资产
    # 做单元测试时（build_asset_context）没有真实的 DagsterRun，context.run 会抛错。
    # op_execution_context.run_id 在两种场景下都可用，且不产生弃用告警。
    return context.op_execution_context.run_id


def _context_for(context: AssetExecutionContext) -> FetchContext:
    return FetchContext(
        ingest_run_id=_run_id(context),
        partition_date=date.fromisoformat(context.partition_key),
    )


@asset(partitions_def=DAILY, group_name="ingest")
def fact_normalized(
    context: AssetExecutionContext, adapter: ResourceParam[FactAdapter]
) -> list[FactRecord]:
    """拉取并归一化。不写库——入库是下一个资产的事。"""
    fetch_ctx = _context_for(context)
    records: list[FactRecord] = []
    for raw in adapter.fetch(fetch_ctx):
        records.extend(adapter.parse(raw))
    context.log.info("normalized", extra={"run_id": fetch_ctx.ingest_run_id, "count": len(records)})
    return records


@asset(partitions_def=DAILY, group_name="ingest")
def fin_fact_loaded(
    context: AssetExecutionContext,
    fact_normalized: list[FactRecord],
    writer: ResourceParam[PointInTimeWriter],
) -> dict[WriteOutcome, int]:
    """经时点写入中间件入库。重跑同一分区时全部走 SKIPPED_IDENTICAL。"""
    counts = writer.write_facts(fact_normalized)
    context.log.info(
        "loaded",
        extra={
            "run_id": _run_id(context),
            "as_of": None,
            "counts": {k.value: v for k, v in counts.items()},
        },
    )
    return counts
