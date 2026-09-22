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

import psycopg
from dagster import AssetExecutionContext, DailyPartitionsDefinition, ResourceParam, asset

from ragdemo.adapters.base import FactAdapter, FactRecord, FetchContext
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.adapters.tushare import PriceRow, TushareAdapter
from ragdemo.ingest.reconcile import read_watermark, write_watermark
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


# --- 行情（阶段 F：F4，core.price_daily 此前零接入代码）---------------------

TUSHARE_SOURCE_ID = "tushare"


@asset(partitions_def=DAILY, group_name="ingest")
def price_normalized(
    context: AssetExecutionContext,
    gateway: ResourceParam[GatewayClient],
    conn: ResourceParam[psycopg.Connection],
) -> list[PriceRow]:
    """给 `core.entity` 里每个登记了 `tushare_code` 的实体拉一次行情，经
    MCP 网关的 `daily` 工具（`TushareAdapter.fetch_daily`，见该方法 docstring
    的三条实测细节）。

    watermark（F2）：读回这个源截至本分区为止最新的游标，传给
    `fetch_daily`——没有游标时退化为只拉 partition_date 当天；游标已经
    覆盖到这一天时（同一分区重跑）不产出任何数据。游标本身在
    `price_daily_loaded` 写库成功之后才推进，不在这里推进——避免"游标已
    推进但数据没写成功"这类丢数据窗口。
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
    adapter = TushareAdapter()
    rows: list[PriceRow] = []
    for ts_code in ts_codes:
        for raw in adapter.fetch_daily(fetch_ctx, gateway, ts_code=ts_code):
            rows.extend(adapter.parse_prices(raw))
    context.log.info(
        "price normalized",
        extra={
            "run_id": fetch_ctx.ingest_run_id,
            "watermark": watermark,
            "entity_count": len(ts_codes),
            "count": len(rows),
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
