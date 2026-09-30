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

from dataclasses import replace
from datetime import date

import psycopg
from dagster import AssetExecutionContext, DailyPartitionsDefinition, ResourceParam, asset

from ragdemo.adapters.base import FactAdapter, FactRecord, FetchContext
from ragdemo.adapters.tushare import PriceRow
from ragdemo.ingest.prices import PriceSources
from ragdemo.ingest.reconcile import read_watermark, write_watermark
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.ingest.writer import PointInTimeWriter, WriteOutcome
from ragdemo_core.blob import BlobStore

# end_offset=1：把"今天"这个分区也算进来。默认只有已经结束的日子才是分区，盘后 17:00
# 的行情调度请求当天分区会报 DagsterUnknownPartitionError（2026-09-30 测试暴露）。
DAILY = DailyPartitionsDefinition(start_date="2022-01-01", timezone="Asia/Shanghai", end_offset=1)


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
    context: AssetExecutionContext,
    adapter: ResourceParam[FactAdapter],
    conn: ResourceParam[psycopg.Connection],
    blob: ResourceParam[BlobStore],
) -> list[FactRecord]:
    """拉取并归一化。每次原始响应先落 provider_snapshot（CLAUDE.md §1.5）再解析，
    解析出的每条记录带上 snapshot_id；事实本身不在这里写库——那是下一个资产的事。"""
    fetch_ctx = _context_for(context)
    records: list[FactRecord] = []
    for raw in adapter.fetch(fetch_ctx):
        snapshot_id = record_snapshot(conn, raw, ingest_run_id=fetch_ctx.ingest_run_id, blob=blob)
        records.extend(replace(r, snapshot_id=snapshot_id) for r in adapter.parse(raw))
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


# --- 行情（阶段 F：F4，core.price_daily 此前零接入代码）---------------------

TUSHARE_SOURCE_ID = "tushare"


@asset(partitions_def=DAILY, group_name="ingest")
def price_normalized(
    context: AssetExecutionContext,
    price_sources: ResourceParam[PriceSources],
    conn: ResourceParam[psycopg.Connection],
    blob: ResourceParam[BlobStore],
) -> list[PriceRow]:
    """给 `core.entity` 里每个登记了 `tushare_code` 的实体拉一次行情，按 ADR-0012 的三层
    降级取数（Tushare MCP → Tushare REST → Wind 日 K 线，见 `ingest/prices.py`）。

    某个实体所有层都不可用时整个分区失败（`AllLayersFailed` 原样抛出）：游标不推进，
    下次重拉——不交出缺了实体的半份行情，下游会以为数据完整。

    watermark（F2）：读回这个源截至本分区为止最新的游标，传给各层——没有游标时退化为
    只拉 partition_date 当天；游标已经覆盖到这一天时（同一分区重跑）不产出任何数据。
    游标本身在 `price_daily_loaded` 写库成功之后才推进，不在这里推进。
    """
    partition_date = date.fromisoformat(context.partition_key)
    watermark = read_watermark(conn, TUSHARE_SOURCE_ID, partition_date)
    fetch_ctx = FetchContext(
        ingest_run_id=_run_id(context), partition_date=partition_date, watermark=watermark
    )
    ts_codes = [
        str(r[0])
        for r in conn.execute(
            "SELECT tushare_code FROM core.entity WHERE tushare_code IS NOT NULL"
        ).fetchall()
    ]
    rows: list[PriceRow] = []
    layers: dict[str, int] = {}
    for ts_code in ts_codes:
        routed = price_sources.route_daily(fetch_ctx, conn, blob, ts_code)
        rows.extend(routed.value)
        layers[routed.layer] = layers.get(routed.layer, 0) + 1
        if routed.skipped or routed.degraded:
            context.log.warning(
                "price source fell back",
                extra={
                    "run_id": fetch_ctx.ingest_run_id,
                    "as_of": partition_date.isoformat(),
                    "entity_id": ts_code,
                    "layer": routed.layer,
                    "degraded": routed.degraded,
                    "skipped": [f"{name}: {why}" for name, why in routed.skipped],
                },
            )
    context.log.info(
        "price normalized",
        extra={
            "run_id": fetch_ctx.ingest_run_id,
            "as_of": partition_date.isoformat(),
            "watermark": watermark,
            "entity_count": len(ts_codes),
            "count": len(rows),
            "layers": layers,
        },
    )
    return rows


@asset(partitions_def=DAILY, group_name="ingest")
def price_daily_loaded(
    context: AssetExecutionContext,
    price_normalized: list[PriceRow],
    tushare_writer: ResourceParam[PointInTimeWriter],
    conn: ResourceParam[psycopg.Connection],
) -> dict[WriteOutcome, int]:
    """写库成功后才推进游标到本分区——写库失败时这次 run 会整体失败，
    下次重跑仍然看不到游标推进，会重新拉这一天，这是刻意的（宁可重复
    拉取，不可丢数据）。"""
    counts = tushare_writer.write_prices(price_normalized)
    partition_date = date.fromisoformat(context.partition_key)
    write_watermark(conn, TUSHARE_SOURCE_ID, partition_date, partition_date.strftime("%Y%m%d"))
    context.log.info(
        "price loaded",
        extra={
            "run_id": _run_id(context),
            "counts": {k.value: v for k, v in counts.items()},
        },
    )
    return counts
