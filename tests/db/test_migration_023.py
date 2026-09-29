"""迁移 023：阶段二三张时点事实表（股东 / 分部 / 汇率）的表级约束。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
NOW = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer, l2_segment,"
            " l3_node, primary_node) VALUES ('CN.688041', '海光信息', 'listed', '算力', 'AI芯片',"
            " ARRAY['通用服务器CPU'], '通用服务器CPU')"
        )
        c.execute(
            "INSERT INTO core.provider_snapshot (provider, endpoint, params, response_ref,"
            " ingest_run_id, fetched_at) VALUES ('wind', 't', '{}', 'x', 't', now())"
        )
        c.commit()
        yield c
        c.close()


def _holding(
    conn: psycopg.Connection,
    *,
    ratio: str = "0.2796",
    rank: int = 1,
    superseded_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO core.shareholder_holding (entity_id, period, period_end, rank, holder_name,"
        " shares, ratio, valid_from, known_at, superseded_at, known_at_basis, source, snapshot_id,"
        " ingest_run_id) VALUES ('CN.688041', '2024FY', '2024-12-31', %s, 'X', 100, %s,"
        " '2024-12-31', %s, %s, 'first_periodic_report', 'wind', 1, 't')",
        (rank, ratio, NOW, superseded_at),
    )


@pytest.mark.db
def test_migration_leaves_no_schema_invariant_violations(conn: psycopg.Connection) -> None:
    assert check_schema_invariants(conn) == []


@pytest.mark.db
def test_ratio_must_be_a_fraction_not_a_percentage(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _holding(conn, ratio="27.96")


@pytest.mark.db
def test_rank_is_limited_to_ten(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _holding(conn, rank=11)


@pytest.mark.db
def test_one_live_row_per_rank_but_superseded_rows_may_repeat(conn: psycopg.Connection) -> None:
    _holding(conn, superseded_at=NOW + timedelta(days=1))
    _holding(conn)  # 旧行已被取代，同 rank 的现行行可以再插
    with pytest.raises(psycopg.errors.UniqueViolation):
        _holding(conn)


@pytest.mark.db
def test_superseded_at_must_be_after_known_at(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _holding(conn, superseded_at=NOW)


@pytest.mark.db
def test_fx_rate_must_be_positive_and_unique_per_day(conn: psycopg.Connection) -> None:
    ins = (
        "INSERT INTO core.fx_rate (base_ccy, quote_ccy, rate_date, rate, valid_from, known_at,"
        " source, snapshot_id, ingest_run_id) VALUES ('USD', 'CNY', '2026-03-27', %s,"
        " '2026-03-27', '2026-03-27T01:15:00Z', 'cfets', 1, 't')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, ("0",))
    conn.rollback()
    conn.execute(ins, ("6.9",))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(ins, ("6.8",))
