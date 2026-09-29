"""币种换算：全 Decimal、按 as_of 可见、陈旧报错（阶段二 Task 4）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.structured_rows import FxRow
from ragdemo.ingest.pit_tables import write_fx
from ragdemo.metrics.fx import FxUnavailable, convert
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
OBSERVED = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


def _known(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 1, 15, tzinfo=UTC)  # 09:15 +08:00


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.provider_snapshot (provider, endpoint, params, response_ref,"
            " ingest_run_id, fetched_at) VALUES ('cfets', 't', '{}', 'x', 't', now())"
        )
        sid = c.execute("SELECT max(snapshot_id) FROM core.provider_snapshot").fetchone()
        assert sid is not None
        rows = [
            FxRow("USD", "CNY", date(2026, 9, 25), Decimal("7.0"), _known(date(2026, 9, 25))),
            FxRow("HKD", "CNY", date(2026, 9, 25), Decimal("0.9"), _known(date(2026, 9, 25))),
            FxRow("USD", "CNY", date(2026, 9, 28), Decimal("6.5"), _known(date(2026, 9, 28))),
        ]
        write_fx(
            c,
            rows=rows,
            source="cfets",
            snapshot_id=int(sid[0]),
            ingest_run_id="t",
            observed_at=OBSERVED,
        )
        c.commit()
        yield c
        c.close()


AFTER = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.db
def test_same_currency_is_returned_as_is(conn: psycopg.Connection) -> None:
    assert convert(conn, Decimal("5"), "USD", "USD", date(2026, 9, 28), AFTER) == Decimal("5")


@pytest.mark.db
def test_direct_and_inverse(conn: psycopg.Connection) -> None:
    assert convert(conn, Decimal("100"), "USD", "CNY", date(2026, 9, 28), AFTER) == Decimal("650.0")
    assert convert(conn, Decimal("650"), "CNY", "USD", date(2026, 9, 28), AFTER) == Decimal("100")


@pytest.mark.db
def test_cross_rate_goes_through_cny(conn: psycopg.Connection) -> None:
    # 9-25：1 USD = 7.0 CNY，1 HKD = 0.9 CNY → 90 USD = 630 CNY = 700 HKD
    got = convert(conn, Decimal("90"), "USD", "HKD", date(2026, 9, 25), AFTER)
    assert got == Decimal("700")


@pytest.mark.db
def test_weekend_uses_the_previous_trading_day(conn: psycopg.Connection) -> None:
    assert convert(conn, Decimal("1"), "USD", "CNY", date(2026, 9, 27), AFTER) == Decimal("7.0")


@pytest.mark.db
def test_stale_rate_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(FxUnavailable):
        convert(conn, Decimal("1"), "USD", "CNY", date(2026, 10, 30), AFTER)
    # 放宽阈值才允许
    got = convert(conn, Decimal("1"), "USD", "CNY", date(2026, 10, 30), AFTER, max_stale_days=60)
    assert got == Decimal("6.5")


@pytest.mark.db
def test_unknown_currency_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(FxUnavailable):
        convert(conn, Decimal("1"), "EUR", "CNY", date(2026, 9, 28), AFTER)


@pytest.mark.db
def test_rate_is_invisible_before_it_was_published(conn: psycopg.Connection) -> None:
    """9-28 的中间价 09:15（北京）才公布：as_of 在当天 09:00 只能看到 9-25 的。"""
    early = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)  # 09:00 +08:00
    assert convert(conn, Decimal("1"), "USD", "CNY", date(2026, 9, 28), early) == Decimal("7.0")
    late = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)  # 09:30 +08:00
    assert convert(conn, Decimal("1"), "USD", "CNY", date(2026, 9, 28), late) == Decimal("6.5")
