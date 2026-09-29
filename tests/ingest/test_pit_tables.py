"""三张时点表共用写入器：首次披露 / 无变化 / 更正追加（阶段二 Task 3）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.structured_rows import FxRow, HoldingRow, SegmentRow
from ragdemo.ingest.pit_tables import Outcome, write_fx, write_holdings, write_segments
from ragdemo.ingest.writer import OutOfOrderCorrection
from ragdemo_core.db.migrate import migrate
from ragdemo_core.db.session import as_of_session

MIGRATIONS = Path("db/migrations")
DISCLOSED = datetime(2025, 3, 28, 15, 59, 59, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


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


def _snapshot_id(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT max(snapshot_id) FROM core.provider_snapshot").fetchone()
    assert row is not None
    return int(row[0])


def _holdings(*shares: str) -> list[HoldingRow]:
    return [
        HoldingRow(rank=i + 1, holder_name=f"H{i + 1}", shares=Decimal(s), ratio=Decimal("0.1"))
        for i, s in enumerate(shares)
    ]


def _write(
    conn: psycopg.Connection, rows: list[HoldingRow], observed: datetime = OBSERVED
) -> Outcome:
    return write_holdings(
        conn,
        entity_id="CN.688041",
        period="2024FY",
        period_end=date(2024, 12, 31),
        rows=rows,
        known_at=DISCLOSED,
        source="wind",
        source_ref="42",
        snapshot_id=_snapshot_id(conn),
        ingest_run_id="t",
        observed_at=observed,
    )


def _live(conn: psycopg.Connection) -> list[tuple[int, str, datetime, str]]:
    return conn.execute(
        "SELECT rank, shares::text, known_at, known_at_basis FROM core.shareholder_holding"
        " WHERE superseded_at IS NULL ORDER BY rank"
    ).fetchall()


@pytest.mark.db
def test_first_write_uses_the_first_disclosure_time(conn: psycopg.Connection) -> None:
    assert _write(conn, _holdings("100", "90")) is Outcome.INSERTED
    assert [(r[0], r[2], r[3]) for r in _live(conn)] == [
        (1, DISCLOSED, "first_periodic_report"),
        (2, DISCLOSED, "first_periodic_report"),
    ]


@pytest.mark.db
def test_identical_rerun_writes_nothing(conn: psycopg.Connection) -> None:
    _write(conn, _holdings("100", "90"))
    assert _write(conn, _holdings("100", "90"), LATER) is Outcome.UNCHANGED
    count = conn.execute("SELECT count(*) FROM core.shareholder_holding").fetchone()
    assert count == (2,)


@pytest.mark.db
def test_changed_value_is_appended_as_a_correction_and_history_is_kept(
    conn: psycopg.Connection,
) -> None:
    _write(conn, _holdings("100", "90"))
    assert _write(conn, _holdings("100", "95"), LATER) is Outcome.REVISED
    live = _live(conn)
    assert live[1][1] == "95" and live[1][2] == LATER and live[1][3] == "revision_observed"
    assert live[0][2] == LATER  # 整批换新，第一名同样重新写入

    with as_of_session(conn, datetime(2026, 10, 1, tzinfo=UTC)):  # 更正之前
        before = conn.execute(
            "SELECT rank, shares::text FROM asof.shareholder_holding ORDER BY rank"
        ).fetchall()
    with as_of_session(conn, datetime(2026, 11, 1, tzinfo=UTC)):  # 更正之后
        after = conn.execute(
            "SELECT rank, shares::text FROM asof.shareholder_holding ORDER BY rank"
        ).fetchall()
    assert before == [(1, "100"), (2, "90")]
    assert after == [(1, "100"), (2, "95")]


@pytest.mark.db
def test_a_vanished_row_counts_as_a_change(conn: psycopg.Connection) -> None:
    _write(conn, _holdings("100", "90"))
    assert _write(conn, _holdings("100"), LATER) is Outcome.REVISED
    assert len(_live(conn)) == 1


@pytest.mark.db
def test_correction_observed_before_known_at_is_rejected(conn: psycopg.Connection) -> None:
    _write(conn, _holdings("100"))
    with pytest.raises(OutOfOrderCorrection):
        _write(conn, _holdings("101"), datetime(2025, 1, 1, tzinfo=UTC))


@pytest.mark.db
def test_segments_use_the_same_rules(conn: psycopg.Connection) -> None:
    rows = [
        SegmentRow(
            "product",
            "高端处理器",
            Decimal("9134299100"),
            Decimal("5818376700"),
            Decimal("0.99696"),
        ),
        SegmentRow("region", "中国大陆", Decimal("9162149100"), None, None),
    ]

    def go(rs: list[SegmentRow], observed: datetime) -> Outcome:
        return write_segments(
            conn,
            entity_id="CN.688041",
            period="2024FY",
            period_end=date(2024, 12, 31),
            rows=rs,
            currency="CNY",
            known_at=DISCLOSED,
            source="wind",
            source_ref="42",
            snapshot_id=_snapshot_id(conn),
            ingest_run_id="t",
            observed_at=observed,
        )

    assert go(rows, OBSERVED) is Outcome.INSERTED
    assert go(rows, LATER) is Outcome.UNCHANGED
    changed = [rows[0], SegmentRow("region", "中国大陆", Decimal("9162149101"), None, None)]
    assert go(changed, LATER) is Outcome.REVISED


@pytest.mark.db
def test_fx_rows_are_compared_one_by_one(conn: psycopg.Connection) -> None:
    known = datetime(2026, 9, 28, 1, 15, tzinfo=UTC)
    a = FxRow("USD", "CNY", date(2026, 9, 28), Decimal("6.7399"), known)
    b = FxRow("USD", "CNY", date(2026, 9, 29), Decimal("6.7411"), known)
    sid = _snapshot_id(conn)

    def go(rows: list[FxRow], observed: datetime) -> dict[Outcome, int]:
        return write_fx(
            conn,
            rows=rows,
            source="cfets",
            snapshot_id=sid,
            ingest_run_id="t",
            observed_at=observed,
        )

    assert go([a, b], OBSERVED) == {Outcome.INSERTED: 2, Outcome.UNCHANGED: 0, Outcome.REVISED: 0}
    b2 = FxRow("USD", "CNY", date(2026, 9, 29), Decimal("6.7412"), known)
    assert go([a, b2], LATER) == {Outcome.INSERTED: 0, Outcome.UNCHANGED: 1, Outcome.REVISED: 1}
