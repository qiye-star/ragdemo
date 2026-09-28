"""冷却状态与待补清单落在 PG 里，并且不随入库事务回滚。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo.sources.backlog import Backlog
from ragdemo.sources.failures import Failure, FailureKind
from ragdemo.sources.health import PgSourceHealth
from ragdemo.sources.routing import Capability
from ragdemo_core.db.migrate import migrate

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
HOURLY = Failure(FailureKind.RATE_LIMITED, "频率超限(1次/小时)", timedelta(hours=1))
DOWN = Failure(FailureKind.UNAVAILABLE, "连不上", timedelta(minutes=5))


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, Path("db/migrations"))
        c.commit()
    c = psycopg.connect(temp_db, autocommit=True)
    yield c
    c.close()


@pytest.mark.db
def test_health_round_trip(conn: psycopg.Connection) -> None:
    health = PgSourceHealth(conn)
    assert health.blocked_until("tushare-mcp", "price_daily") is None
    health.record_failure("tushare-mcp", "price_daily", HOURLY, now=NOW)
    assert health.blocked_until("tushare-mcp", "price_daily") == NOW + timedelta(hours=1)
    health.record_success("tushare-mcp", "price_daily", now=NOW + timedelta(hours=2))
    row = conn.execute(
        "SELECT blocked_until, last_failure_kind, last_success_at FROM core.source_health"
    ).fetchone()
    assert row == (None, "rate_limited", NOW + timedelta(hours=2)), "成功清冷却、留失败记录"


@pytest.mark.db
def test_state_needs_autocommit(temp_db: str) -> None:
    with psycopg.connect(temp_db) as c:
        with pytest.raises(ValueError, match="autocommit"):
            PgSourceHealth(c)
        with pytest.raises(ValueError, match="autocommit"):
            Backlog(c)


@pytest.mark.db
def test_backlog_keeps_one_open_item_with_the_earliest_gap(conn: psycopg.Connection) -> None:
    backlog = Backlog(conn)
    cap = Capability.FINANCIALS
    backlog.add(cap, "688041.SH", since=date(2025, 6, 30), failure=DOWN, now=NOW)
    backlog.add(cap, "688041.SH", since=date(2025, 3, 31), failure=HOURLY, now=NOW)
    backlog.add(cap, "000977.SZ", since=date(2025, 6, 30), failure=DOWN, now=NOW)
    items = {i.entity_ref: i for i in backlog.open_items(cap)}
    assert items["688041.SH"].since == date(2025, 3, 31)
    assert items["688041.SH"].attempts == 2 and items["688041.SH"].failure_kind == "rate_limited"
    assert len(items) == 2


@pytest.mark.db
def test_backlog_resolves_only_when_the_gap_is_covered(conn: psycopg.Connection) -> None:
    backlog = Backlog(conn)
    cap = Capability.FINANCIALS
    backlog.add(cap, "688041.SH", since=date(2025, 3, 31), failure=DOWN, now=NOW)
    assert not backlog.resolve(cap, "688041.SH", covered_since=date(2025, 6, 30), now=NOW)
    assert backlog.resolve(cap, "688041.SH", covered_since=date(2024, 12, 31), now=NOW)
    assert backlog.open_items(cap) == []
    backlog.add(cap, "688041.SH", since=date(2026, 3, 31), failure=DOWN, now=NOW)
    assert len(backlog.open_items(cap)) == 1, "解决后再缺，要能开一条新的"
