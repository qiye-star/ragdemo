"""月度成本预算（docs/04-ingestion.md §5.3，总计划阶段 1 任务 1.6）。

账本就是 core.provider_snapshot.cost_cents：每一次计费的供应商调用都在那里留了
一行（任务 1.1）。本月（北京时间自然月）已用 ≥ 80% 告警，≥ 100% 在发起下一次计费
调用之前就阻断。单价未知的调用记 NULL，单独计数，不能当成免费。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dagster import build_asset_context

from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.ingest.assets_docs import doc_prepared
from ragdemo.parse.textin import MockDocumentParser, ParseResult
from ragdemo.quality.budget import (
    BudgetExceeded,
    BudgetLevel,
    ensure_budget,
    month_to_date,
    record_budget_metric,
)
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=CST)
CAP = Decimal(5000)


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    c.commit()
    yield c
    c.close()


def _spend(conn: psycopg.Connection, cny: Decimal | None, *, at: datetime = NOW) -> None:
    conn.execute(
        "INSERT INTO core.provider_snapshot (provider, endpoint, params, response_ref,"
        " http_status, ingest_run_id, cost_cents, fetched_at)"
        " VALUES ('textin', 'pdf_to_markdown', '{}', 'x.json', 200, 't', %s, %s)",
        (None if cny is None else cny * 100, at),
    )
    conn.commit()


@pytest.mark.db
def test_no_spend_is_ok(conn: psycopg.Connection) -> None:
    status = month_to_date(conn, now=NOW, cap_cny=CAP)
    assert status.level is BudgetLevel.OK
    assert status.spent_cny == 0 and status.unknown_cost_calls == 0


@pytest.mark.db
def test_eighty_percent_warns_but_does_not_block(conn: psycopg.Connection) -> None:
    _spend(conn, Decimal(4100))
    status = ensure_budget(conn, now=NOW, cap_cny=CAP)
    assert status.level is BudgetLevel.WARN
    assert status.ratio == Decimal("0.82")


@pytest.mark.db
def test_reaching_the_cap_blocks(conn: psycopg.Connection) -> None:
    _spend(conn, Decimal(3000))
    _spend(conn, Decimal(2000))
    assert month_to_date(conn, now=NOW, cap_cny=CAP).level is BudgetLevel.EXCEEDED
    with pytest.raises(BudgetExceeded, match="5000"):
        ensure_budget(conn, now=NOW, cap_cny=CAP)


@pytest.mark.db
def test_month_boundary_is_beijing_time(conn: psycopg.Connection) -> None:
    """8 月 31 日 23:30（北京时间）属于上个月；
    9 月 1 日 00:30（北京时间，UTC 仍是 8 月）属于本月。"""
    _spend(conn, Decimal(4000), at=datetime(2026, 8, 31, 23, 30, tzinfo=CST))
    _spend(conn, Decimal(100), at=datetime(2026, 9, 1, 0, 30, tzinfo=CST))
    status = month_to_date(conn, now=NOW, cap_cny=CAP)
    assert status.spent_cny == Decimal(100)


@pytest.mark.db
def test_calls_with_unknown_cost_are_counted_not_treated_as_free(
    conn: psycopg.Connection,
) -> None:
    _spend(conn, None)
    _spend(conn, None)
    status = month_to_date(conn, now=NOW, cap_cny=CAP)
    assert status.unknown_cost_calls == 2
    assert status.level is BudgetLevel.UNKNOWN


@pytest.mark.db
def test_budget_metric_is_recorded_and_fails_from_the_warning_line(
    conn: psycopg.Connection,
) -> None:
    _spend(conn, Decimal(4100))
    record_budget_metric(conn, month_to_date(conn, now=NOW, cap_cny=CAP), partition=NOW.date())
    row = conn.execute(
        "SELECT value, passed, threshold, note FROM quality.quality_metric"
        " WHERE metric = 'monthly_cost_ratio'"
    ).fetchone()
    assert row is not None
    value, passed, threshold, note = row
    assert float(value) == pytest.approx(0.82) and passed is False and float(threshold) == 0.8
    assert "4100" in note and "5000" in note


# --- 阻断发生在发起计费调用之前 ---------------------------------------------------


class _CountingParser(MockDocumentParser):
    calls = 0

    def parse(self, file_bytes: bytes, *, owner_user: str | None = None) -> ParseResult:
        type(self).calls += 1
        return super().parse(file_bytes, owner_user=owner_user)


@pytest.mark.db
def test_exceeded_budget_stops_document_parsing_before_any_call(
    conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGDEMO_MONTHLY_BUDGET_CNY", "100")
    _spend(conn, Decimal(150), at=datetime.now(CST))
    parser = _CountingParser()
    ctx = build_asset_context(partition_key="2024-10-28")
    with pytest.raises(BudgetExceeded):
        doc_prepared(ctx, [], parser, LocalBlobStore(tmp_path), MockAnnouncementProvider(), conn)
    assert _CountingParser.calls == 0


@pytest.mark.db
def test_docs_ingest_cli_refuses_to_spend_once_the_budget_is_used_up(
    conn: psycopg.Connection, temp_db: str, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from ragdemo.cli import main
    from tests.ingest.test_docs_cli import _write_manifest

    _spend(conn, Decimal(150), at=datetime.now(CST))
    manifest = _write_manifest(tmp_path, count=1)
    result = CliRunner().invoke(
        main,
        [
            "docs",
            "ingest",
            "--manifest",
            str(manifest),
            "--entity-ref",
            "688041.SH",
            "--parser",
            "textin",
            "--blob-root",
            str(tmp_path / "blob"),
            "--yes",
        ],
        env={
            "RAGDEMO_DSN": temp_db,
            "TEXTIN_BASE_URL": "http://unused.invalid",
            "RAGDEMO_MONTHLY_BUDGET_CNY": "100",
        },
    )
    assert result.exit_code != 0
    assert "预算" in result.output
    row = conn.execute("SELECT count(*) FROM core.document").fetchone()
    assert row is not None and row[0] == 0
