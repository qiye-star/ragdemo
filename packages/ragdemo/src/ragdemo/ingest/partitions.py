"""Dagster 分区键与业务时间窗口的换算。

`doc_normalized`（`assets_docs.py`，用它去查供应商）与质量检查
（`quality/checks.py`，用它去圈"这个分区对应的文档"）必须用**同一个**
`(since, until]` 窗口——两处如果各自算一遍，窗口稍有出入就会导致资产实际
处理的文档集合与检查读到的不是同一批，指标失真却不会报错。

本文件刻意不用 `from __future__ import annotations`：与 `assets_docs.py`
同样的原因——Dagster 在 `@asset`/`@asset_check` 装饰期对 `context` 参数做
类型校验时直接比较 `Parameter.annotation`，不解析 PEP 563 的延迟字符串
注解。Python 3.11 原生支持 `tuple[...]` / `X | Y`，去掉这行不影响其余写法。
"""

from datetime import UTC, date, datetime, timedelta

from dagster import AssetCheckExecutionContext, AssetExecutionContext

LOOKBACK = timedelta(days=1)


def partition_window(
    context: AssetExecutionContext | AssetCheckExecutionContext,
) -> tuple[datetime, datetime]:
    """把分区键翻成 `(since, until]` 窗口。"""
    partition = date.fromisoformat(context.partition_key)
    until = datetime.combine(partition, datetime.max.time(), tzinfo=UTC)
    since = until - LOOKBACK
    return since, until
