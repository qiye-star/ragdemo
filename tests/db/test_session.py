"""as_of_session：把时点绑定在事务上，退出即失效。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate
from ragdemo_core.db.session import NaiveDatetimeError, as_of_session

MIGRATIONS = Path("db/migrations")
AS_OF = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.commit()
        yield conn


def test_naive_datetime_is_rejected() -> None:
    """naive datetime 的行为随服务器时区变化，必须在入口拒绝。"""
    with pytest.raises(NaiveDatetimeError), as_of_session(None, datetime(2024, 12, 31)):  # type: ignore[arg-type]
        pass


@pytest.mark.db
def test_as_of_is_visible_inside_session(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with as_of_session(db, AS_OF) as conn:
        row = conn.execute("SELECT asof.current_as_of()").fetchone()
    assert row is not None
    assert row[0] == AS_OF


@pytest.mark.db
def test_as_of_does_not_leak_after_session(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """连接池复用时最容易出的 bug：上一个请求的 as_of 泄漏给下一个。"""
    with as_of_session(db, AS_OF):
        pass
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        db.execute("SELECT asof.current_as_of()").fetchone()


@pytest.mark.db
def test_tenant_and_user_are_set(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with as_of_session(db, AS_OF, tenant="t1", user="u1") as conn:
        row = conn.execute(
            "SELECT current_setting('app.tenant'), current_setting('app.user')"
        ).fetchone()
    assert row == ("t1", "u1")
