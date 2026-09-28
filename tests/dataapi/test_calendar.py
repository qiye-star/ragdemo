"""交易日历（docs/03-point-in-time.md §6.4）：as_of 落在非交易日时取之前最近的交易日。

日历从 `asof.price_daily` 的实际行情推导——某市场有行情的日期即交易日——不引入
额外的日历数据源。走 asof 视图意味着"今天收盘前，今天还不是一个已知的交易日"，
这正是时点语义要的结果。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from ragdemo.dataapi.calendar import last_trading_day
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
CST = timezone(timedelta(hours=8))
ET = timezone(timedelta(hours=-4))

CN = ("SSE", "SZSE")


def _entity(conn: psycopg.Connection, entity_id: str, market: str) -> None:
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node)"
        " VALUES (%s, %s, 'listed', %s, '算力', 'AI芯片', ARRAY['云端训练芯片'], '云端训练芯片')",
        (entity_id, entity_id, market),
    )


def _quote(
    conn: psycopg.Connection,
    entity_id: str,
    day: date,
    *,
    close_at: time = time(15, 30),
    tz: timezone = CST,
    suspended: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO core.price_daily (entity_id, trade_date, close, is_suspended,"
        " valid_from, known_at, source, ingest_run_id)"
        " VALUES (%s, %s, 10, %s, %s, %s, 'test', 'r1')",
        (entity_id, day, suspended, day, datetime.combine(day, close_at, tz)),
    )


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    _entity(c, "CN.688041", "SSE")
    _entity(c, "CN.000977", "SZSE")
    _entity(c, "US.NVDA", "NASDAQ")
    # A 股：2024-09-30（周一）→ 国庆 10-01..10-07 休市 → 10-08 复市；
    # 10-25（周五）→ 周末 → 10-28（周一）；12-31 → 元旦 → 2025-01-02。
    for d in [date(2024, 9, 30), date(2024, 10, 8), date(2024, 10, 25), date(2024, 10, 28)]:
        _quote(c, "CN.688041", d)
    _quote(c, "CN.000977", date(2024, 12, 31))  # 只有深市这只票有这天的行情
    _quote(c, "CN.000977", date(2025, 1, 2))
    # 美股在 A 股国庆期间照常交易——不能让它把 A 股的休市日变成交易日。
    _quote(c, "US.NVDA", date(2024, 10, 3), close_at=time(16, 0), tz=ET)
    c.commit()
    yield c
    c.close()


@pytest.mark.db
def test_weekend_falls_back_to_friday(conn: psycopg.Connection) -> None:
    as_of = datetime(2024, 10, 27, 12, 0, tzinfo=CST)  # 周日
    assert last_trading_day(conn, as_of=as_of, markets=CN) == date(2024, 10, 25)


@pytest.mark.db
def test_holiday_falls_back_to_last_day_before_it(conn: psycopg.Connection) -> None:
    as_of = datetime(2024, 10, 5, 12, 0, tzinfo=CST)  # 国庆中
    assert last_trading_day(conn, as_of=as_of, markets=CN) == date(2024, 9, 30)


@pytest.mark.db
def test_year_boundary_falls_back_into_previous_year(conn: psycopg.Connection) -> None:
    as_of = datetime(2025, 1, 1, 12, 0, tzinfo=CST)  # 元旦
    assert last_trading_day(conn, as_of=as_of, markets=CN) == date(2024, 12, 31)


@pytest.mark.db
def test_trading_day_is_not_known_before_its_close(conn: psycopg.Connection) -> None:
    """盘中的 as_of 看不到当天收盘行情，因此当天还不算"已知的交易日"。
    取之前、不取之后——这里的"之后"就是还没发生的收盘。"""
    before_close = datetime(2024, 10, 28, 10, 0, tzinfo=CST)
    after_close = datetime(2024, 10, 28, 16, 0, tzinfo=CST)
    assert last_trading_day(conn, as_of=before_close, markets=CN) == date(2024, 10, 25)
    assert last_trading_day(conn, as_of=after_close, markets=CN) == date(2024, 10, 28)


@pytest.mark.db
def test_other_markets_do_not_make_a_holiday_a_trading_day(conn: psycopg.Connection) -> None:
    as_of = datetime(2024, 10, 5, 12, 0, tzinfo=CST)
    assert last_trading_day(conn, as_of=as_of, markets=CN) == date(2024, 9, 30)
    assert last_trading_day(conn, as_of=as_of, markets=("NASDAQ",)) == date(2024, 10, 3)


@pytest.mark.db
def test_suspended_quotes_do_not_count(conn: psycopg.Connection) -> None:
    _quote(conn, "CN.688041", date(2024, 10, 26), suspended=True)  # 周六的停牌行，不该存在
    conn.commit()
    as_of = datetime(2024, 10, 27, 12, 0, tzinfo=CST)
    assert last_trading_day(conn, as_of=as_of, markets=CN) == date(2024, 10, 25)


@pytest.mark.db
def test_no_quotes_before_as_of_returns_none(conn: psycopg.Connection) -> None:
    as_of = datetime(2024, 1, 1, 12, 0, tzinfo=CST)
    assert last_trading_day(conn, as_of=as_of, markets=CN) is None


@pytest.mark.db
def test_unknown_market_returns_none(conn: psycopg.Connection) -> None:
    as_of = datetime(2024, 10, 30, 12, 0, tzinfo=CST)
    assert last_trading_day(conn, as_of=as_of, markets=("KRX",)) is None


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        last_trading_day(None, as_of=datetime(2024, 10, 27), markets=CN)  # type: ignore[arg-type]


def test_empty_markets_is_rejected() -> None:
    with pytest.raises(ValueError, match="markets"):
        last_trading_day(None, as_of=datetime(2024, 10, 27, tzinfo=CST), markets=())  # type: ignore[arg-type]
