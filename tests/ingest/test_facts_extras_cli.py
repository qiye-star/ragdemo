"""`ragdemo facts fx / holders / segments` 的 dry run：不调外部、不写库，给出额度上限。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from ragdemo.ingest.facts_cli import facts_group
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def dsn(temp_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type, market,"
            " l1_layer, l2_segment, l3_node, primary_node, tushare_code) VALUES"
            " ('CN.688041', '海光信息技术股份有限公司', '海光信息', 'listed', 'SSE', '算力',"
            " 'AI芯片', ARRAY['通用服务器CPU'], '通用服务器CPU', '688041.SH')"
        )
        day = date(2025, 3, 1)
        title = "海光信息技术股份有限公司2024年年度报告"
        c.execute(
            "INSERT INTO core.announcement_index (entity_id, source, source_ref, title,"
            " title_norm, publish_date, has_full_text, valid_from, known_at, ingest_run_id)"
            " VALUES ('CN.688041', 'cninfo', '1', %s, %s, %s, false, %s, %s, 't')",
            (title, title, day, day, datetime(2025, 3, 1, 15, 59, 59, tzinfo=UTC)),
        )
        c.commit()
    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    yield temp_db


@pytest.mark.db
def test_holders_dry_run_reports_the_wind_call_ceiling(dsn: str) -> None:
    result = CliRunner().invoke(
        facts_group,
        ["holders", "--entity-ref", "688041.SH", "--period", "2024FY", "--period", "2023FY"],
    )
    assert result.exit_code == 0, result.output
    assert "最多调用 Wind 1 次" in result.output  # 2023FY 没有披露记录，不计入
    assert "无披露记录跳过 1" in result.output
    with psycopg.connect(dsn) as c:
        n = c.execute("SELECT count(*) FROM core.provider_snapshot").fetchone()
        assert n == (0,)


@pytest.mark.db
def test_segments_dry_run(dsn: str) -> None:
    result = CliRunner().invoke(
        facts_group, ["segments", "--entity-ref", "688041.SH", "--period", "2024FY"]
    )
    assert result.exit_code == 0 and "最多调用 Wind 1 次" in result.output


def test_fx_dry_run_makes_no_request() -> None:
    result = CliRunner().invoke(
        facts_group, ["fx", "--since", "2023-01-01", "--until", "2026-09-29"]
    )
    assert result.exit_code == 0
    assert "5 个币种对" in result.output and "dry run" in result.output
