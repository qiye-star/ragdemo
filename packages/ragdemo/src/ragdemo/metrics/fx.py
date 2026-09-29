"""币种换算（阶段二 Task 4）：全部 `Decimal`，只读 `asof.fx_rate`。

规则：
- 同币种原样返回；
- 取 `rate_date <= on` 的**最近一条**（周末、节假日用上一个交易日）；
- 最近一条比 `on` 早超过 `max_stale_days` 天，抛 `FxUnavailable`——不静默用陈旧汇率；
- 只有 `known_at <= as_of` 的汇率可见（`asof` 视图保证）：当天 09:00 的 `as_of`
  看不到当天 09:15 才公布的中间价；
- 经人民币交叉：非 CNY 互转走 `from → CNY → to`。
- 结果不四舍五入——取整由展示层决定，计算链路保留 Decimal 全精度。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import psycopg

from ragdemo_core.db.session import as_of_session

PIVOT = "CNY"


class FxUnavailable(LookupError):
    """在给定 as_of 下，没有可用（或足够新）的汇率。"""


def _to_cny_rate(
    conn: psycopg.Connection, ccy: str, on: date, max_stale_days: int
) -> tuple[Decimal, date]:
    """1 单位 `ccy` 折多少人民币，返回 (汇率, 该汇率的日期)；须在 as_of_session 内调用。"""
    row = conn.execute(
        "SELECT rate, rate_date FROM asof.fx_rate"
        " WHERE base_ccy = %s AND quote_ccy = %s AND rate_date <= %s"
        " ORDER BY rate_date DESC LIMIT 1",
        (ccy, PIVOT, on),
    ).fetchone()
    if row is None:
        raise FxUnavailable(f"{ccy}/{PIVOT}：在该 as_of 下没有 {on} 及之前的汇率")
    rate, rate_date = Decimal(row[0]), row[1]
    if (on - rate_date).days > max_stale_days:
        raise FxUnavailable(
            f"{ccy}/{PIVOT}：最近的汇率是 {rate_date}，距 {on} 已超过 {max_stale_days} 天"
        )
    return rate, rate_date


def convert(
    conn: psycopg.Connection,
    amount: Decimal,
    from_ccy: str,
    to_ccy: str,
    on: date,
    as_of: datetime,
    *,
    max_stale_days: int = 7,
) -> Decimal:
    if from_ccy == to_ccy:
        return amount
    with as_of_session(conn, as_of):
        result = amount
        if from_ccy != PIVOT:
            result = result * _to_cny_rate(conn, from_ccy, on, max_stale_days)[0]
        if to_ccy != PIVOT:
            result = result / _to_cny_rate(conn, to_ccy, on, max_stale_days)[0]
    return result
