"""手工录入产业指标：校验一次报全、来源必填、单位严格、留底快照、更正走时点规则。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from ragdemo.cli import main
from ragdemo.ingest.writer import WriteOutcome
from ragdemo.metrics.manual import COLUMNS, ManualImportError, import_rows, parse_rows
from ragdemo.seed.loader import load_all
from ragdemo_core.db.migrate import migrate

TODAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
HEADER = ",".join(COLUMNS)
GOOD = "nvda_datacenter_revenue,US.NVDA,2026Q2,2026-07-31,41000000000,USD,2026-08-27,NVDA 10-Q,p.12"


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    load_all(c, Path("db/seed"))
    yield c
    c.close()


def _csv(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "m.csv"
    p.write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return p


def test_a_valid_row_is_imported_with_snapshot_and_day_precision_known_at(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    path = _csv(tmp_path, GOOD)
    rows = parse_rows(conn, path, today=TODAY)
    assert import_rows(conn, path, rows, now=NOW) == {WriteOutcome.INSERTED: 1}
    row = conn.execute(
        "SELECT f.value, f.known_at, f.source, f.source_ref, s.provider FROM core.fin_fact f"
        " JOIN core.provider_snapshot s USING (snapshot_id) WHERE f.metric_id = %s",
        ("nvda_datacenter_revenue",),
    ).fetchone()
    assert row is not None
    assert int(row[0]) == 41000000000
    assert row[1] == datetime(
        2026, 8, 27, 15, 59, 59, 999999, tzinfo=UTC
    )  # 当日 23:59:59.999999 CST
    assert (row[2], row[3], row[4]) == ("manual", "NVDA 10-Q | p.12", "manual")


def test_all_problems_are_reported_at_once_and_nothing_is_written(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    path = _csv(
        tmp_path,
        "no_such_metric,US.NVDA,2026Q2,2026-07-31,1,USD,2026-08-27,r,p",
        "gross_margin,US.NVDA,2026Q2,2026-07-31,0.7,ratio,2026-08-27,r,p",  # derived
        "nvda_datacenter_revenue,US.NVDA,2026Q2,2026-07-31,1,CNY,2026-08-27,r,p",  # 单位错
        "nvda_datacenter_revenue,US.NOPE,2026Q2,2026-07-31,1,USD,2026-08-27,r,p",  # 实体不存在
        "nvda_datacenter_revenue,US.NVDA,2026Q3,2026-10-31,1,USD,2027-01-01,r,p",  # 未来
        "nvda_datacenter_revenue,US.NVDA,2026Q1,2026-04-30,1,USD,2026-05-28,,",  # 无来源
        "nvda_datacenter_revenue,US.NVDA,2025Q4,2026-01-31,abc,USD,2026-02-26,r,p",  # 非数字
    )
    with pytest.raises(ManualImportError) as exc:
        parse_rows(conn, path, today=TODAY)
    msg = str(exc.value)
    for needle in ("不存在", "推导指标", "单位应为", "未来日期", "不能为空", "不是数字"):
        assert needle in msg
    assert conn.execute(
        "SELECT count(*) FROM core.fin_fact WHERE source = 'manual'"
    ).fetchone() == (0,)


def test_reimport_same_is_skipped_and_a_later_revision_supersedes(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    path = _csv(tmp_path, GOOD)
    rows = parse_rows(conn, path, today=TODAY)
    import_rows(conn, path, rows, now=NOW)
    assert import_rows(conn, path, rows, now=NOW) == {WriteOutcome.SKIPPED_IDENTICAL: 1}

    revised = _csv(
        tmp_path,
        "nvda_datacenter_revenue,US.NVDA,2026Q2,2026-07-31,41500000000,USD,2026-09-10,NVDA 修订,p.3",  # noqa: E501
    )
    assert import_rows(conn, revised, parse_rows(conn, revised, today=TODAY), now=NOW) == {
        WriteOutcome.CORRECTED: 1
    }
    live, old = conn.execute(
        "SELECT count(*) FILTER (WHERE superseded_at IS NULL), count(*) FROM core.fin_fact"
        " WHERE metric_id = 'nvda_datacenter_revenue'"
    ).fetchone() or (0, 0)
    assert (live, old) == (1, 2)


def test_cli_dry_run_writes_nothing_and_yes_writes(
    temp_db: str, conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.commit()
    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    path = _csv(tmp_path, GOOD)
    dry = CliRunner().invoke(main, ["metrics", "import-manual", "--file", str(path)])
    assert dry.exit_code == 0 and "dry run" in dry.output
    assert conn.execute("SELECT count(*) FROM core.fin_fact WHERE source='manual'").fetchone() == (
        0,
    )
    real = CliRunner().invoke(main, ["metrics", "import-manual", "--file", str(path), "--yes"])
    assert real.exit_code == 0, real.output
    assert conn.execute("SELECT count(*) FROM core.fin_fact WHERE source='manual'").fetchone() == (
        1,
    )
