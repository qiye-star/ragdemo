"""交易日历（docs/03-point-in-time.md §6.4）。

日历从 `asof.price_daily` 的实际行情推导：某市场有未停牌行情的日期即交易日，
不引入额外的日历数据源。因为读的是 asof 视图，收盘前的 as_of 看不到当天行情，
当天自然还不算一个已知的交易日——"取之前、不取之后"由时点过滤本身保证。

数据稀疏时（接入的实体很少），某个交易日若恰好所有实体都没有行情，会被误判为
休市。这是"从行情推导"这一设计的已知代价，覆盖面扩大后自然消失。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import psycopg

from ragdemo_core.db.session import as_of_session

_LAST_TRADING_DAY_SQL = (
    "SELECT max(p.trade_date)"
    "  FROM asof.price_daily p"
    "  JOIN core.entity e ON e.entity_id = p.entity_id"
    " WHERE e.market = ANY(%s)"
    "   AND NOT p.is_suspended"
)


def last_trading_day(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    as_of: datetime,
    markets: Sequence[str],
) -> date | None:
    """`as_of` 时刻已知的、`markets` 中最近一个交易日；没有任何已知行情时返回 None。

    Args:
        conn: 数据库连接。
        as_of: 查询时点，必须带时区。
        markets: 交易所代码（`core.entity.market`，如 `("SSE", "SZSE")`）。
            同一日历下的多个交易所要一起传——沪深两市共用一个 A 股日历。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of 必须带时区，收到 naive datetime: {as_of!r}")
    if not markets:
        raise ValueError("markets 不能为空：交易日历总是相对某个市场而言")
    with as_of_session(conn, as_of) as session:
        row = session.execute(_LAST_TRADING_DAY_SQL, (list(markets),)).fetchone()
    day = row[0] if row else None
    assert day is None or isinstance(day, date)
    return day
