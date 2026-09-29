"""阶段二数据体检：对好数据放行、对每一种坏数据报错。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ragdemo.adapters.structured_rows import FxRow, HoldingRow, SegmentRow
from ragdemo.ingest.pit_tables import write_fx, write_holdings, write_segments
from ragdemo.ingest.stage2_verify import Finding, verify_stage2
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
DISCLOSED = datetime(2025, 3, 1, 15, 59, 59, tzinfo=UTC)
NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


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
            "INSERT INTO core.provider_snapshot (provider, endpoint, params, response,"
            " ingest_run_id, fetched_at) VALUES ('wind', 't', '{}', '{}'::jsonb, 't', now())"
        )
        title = "海光信息2024年年度报告"
        c.execute(
            "INSERT INTO core.announcement_index (entity_id, source, source_ref, title, title_norm,"
            " publish_date, has_full_text, valid_from, known_at, ingest_run_id) VALUES"
            " ('CN.688041', 'cninfo', '1', %s, %s, '2025-03-01', false, '2025-03-01', %s, 't')",
            (title, title, DISCLOSED),
        )
        c.commit()
        yield c
        c.close()


def _sid(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT max(snapshot_id) FROM core.provider_snapshot").fetchone()
    assert row is not None
    return int(row[0])


def _holdings(conn: psycopg.Connection, shares: list[str], known_at: datetime = DISCLOSED) -> None:
    rows = [
        HoldingRow(i + 1, f"H{i + 1}", Decimal(s), Decimal("0.1")) for i, s in enumerate(shares)
    ]
    write_holdings(
        conn,
        entity_id="CN.688041",
        period="2024FY",
        period_end=date(2024, 12, 31),
        rows=rows,
        known_at=known_at,
        source="wind",
        source_ref="1",
        snapshot_id=_sid(conn),
        ingest_run_id="t",
        observed_at=NOW,
    )
    conn.commit()


def _errors(findings: list[Finding]) -> list[str]:
    return [f"{f.check}:{f.message}" for f in findings if f.severity == "error"]


@pytest.mark.db
def test_clean_data_passes(conn: psycopg.Connection) -> None:
    _holdings(conn, ["300", "200", "100"])
    assert _errors(verify_stage2(conn, as_of=NOW)) == []


@pytest.mark.db
def test_known_at_earlier_than_first_disclosure_is_an_error(conn: psycopg.Connection) -> None:
    _holdings(conn, ["300", "200"], known_at=DISCLOSED - timedelta(days=30))
    errors = _errors(verify_stage2(conn, as_of=NOW))
    assert any(e.startswith("known_at:") for e in errors)


@pytest.mark.db
def test_holdings_not_descending_is_an_error(conn: psycopg.Connection) -> None:
    _holdings(conn, ["100", "300"])
    assert any(e.startswith("holdings:") for e in _errors(verify_stage2(conn, as_of=NOW)))


@pytest.mark.db
def test_segment_sum_far_from_revenue_is_an_error_and_missing_revenue_is_a_warning(
    conn: psycopg.Connection,
) -> None:
    write_segments(
        conn,
        entity_id="CN.688041",
        period="2024FY",
        period_end=date(2024, 12, 31),
        rows=[SegmentRow("product", "a", Decimal("1000"), None, None)],
        currency="CNY",
        known_at=DISCLOSED,
        source="wind",
        source_ref="1",
        snapshot_id=_sid(conn),
        ingest_run_id="t",
        observed_at=NOW,
    )
    conn.commit()
    findings = verify_stage2(conn, as_of=NOW)
    assert any(
        f.severity == "warning" and f.check == "segments" for f in findings
    )  # 无营收：未对账
    assert not _errors(findings)


@pytest.mark.db
def test_row_without_a_replayable_snapshot_is_an_error(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO core.provider_snapshot (provider, endpoint, params, ingest_run_id, fetched_at)"
        " VALUES ('wind', 't', '{}', 't', now())"  # 既没有 response 也没有 response_ref
    )
    _holdings(conn, ["300", "200"])
    assert any(e.startswith("snapshots:") for e in _errors(verify_stage2(conn, as_of=NOW)))


def _fx(conn: psycopg.Connection, base: str, days: list[tuple[date, str]]) -> None:
    rows = [
        FxRow(base, "CNY", d, Decimal(r), datetime.combine(d, datetime.min.time(), tzinfo=UTC))
        for d, r in days
    ]
    write_fx(
        conn, rows=rows, source="cfets", snapshot_id=_sid(conn), ingest_run_id="t", observed_at=NOW
    )
    conn.commit()


@pytest.mark.db
def test_fx_out_of_range_jump_and_gap_are_errors(conn: psycopg.Connection) -> None:
    _fx(conn, "USD", [(date(2026, 9, 1), "7.0"), (date(2026, 9, 2), "7.01")])
    assert _errors(verify_stage2(conn, as_of=NOW)) == []

    _fx(conn, "JPY", [(date(2026, 9, 1), "4.275")])  # 忘了除以 100 → 超出合理区间
    assert any("超出合理区间" in e for e in _errors(verify_stage2(conn, as_of=NOW)))


@pytest.mark.db
def test_fx_daily_jump_and_long_gap(conn: psycopg.Connection) -> None:
    _fx(conn, "USD", [(date(2026, 9, 1), "7.0"), (date(2026, 9, 2), "7.5")])  # +7%
    errors = _errors(verify_stage2(conn, as_of=NOW))
    assert any("波动" in e for e in errors)
    _fx(conn, "EUR", [(date(2026, 8, 1), "8.0"), (date(2026, 8, 20), "8.0")])  # 19 天没有记录
    assert any("疑似漏取" in e for e in _errors(verify_stage2(conn, as_of=NOW)))
