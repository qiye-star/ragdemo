"""报告期首次披露时刻：从公告目录求某期定期报告最早何时可获知（阶段二 Task 2）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.period_disclosure import first_disclosure, period_end_of
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer, l2_segment,"
            " l3_node, primary_node) VALUES ('CN.688041', '海光信息', 'listed', '算力', 'AI芯片',"
            " ARRAY['通用服务器CPU'], '通用服务器CPU')"
        )
        c.commit()
        yield c
        c.close()


_n = 0


def _ann(
    conn: psycopg.Connection,
    title: str,
    published: str,
    *,
    source: str = "cninfo",
    superseded: bool = False,
) -> None:
    global _n
    _n += 1
    day = date.fromisoformat(published)
    known = datetime(day.year, day.month, day.day, 15, 59, 59, tzinfo=UTC)
    conn.execute(
        "INSERT INTO core.announcement_index (entity_id, source, source_ref, title, title_norm,"
        " publish_date, has_full_text, valid_from, known_at, superseded_at, ingest_run_id)"
        " VALUES ('CN.688041', %s, %s, %s, %s, %s, false, %s, %s, %s, 't')",
        (
            source,
            f"r{_n}",
            title,
            title,
            day,
            day,
            known,
            datetime(2026, 9, 1, tzinfo=UTC) if superseded else None,
        ),
    )
    conn.commit()


def test_period_end_of() -> None:
    assert period_end_of("2024FY") == date(2024, 12, 31)
    assert period_end_of("2025H1") == date(2025, 6, 30)
    assert period_end_of("2025Q1") == date(2025, 3, 31)
    assert period_end_of("2025Q3") == date(2025, 9, 30)
    with pytest.raises(ValueError):
        period_end_of("2025Q2")


@pytest.mark.db
def test_first_disclosure_prefers_the_original_over_the_correction(
    conn: psycopg.Connection,
) -> None:
    _ann(conn, "海光信息2024年年度报告", "2025-03-28")
    _ann(conn, "海光信息2024年年度报告(更正后)", "2025-04-30")
    d = first_disclosure(conn, "CN.688041", "2024FY")
    assert d is not None and d.known_at.date() == date(2025, 3, 28)


@pytest.mark.db
def test_year_must_match(conn: psycopg.Connection) -> None:
    _ann(conn, "海光信息2024年年度报告", "2025-03-28")
    assert first_disclosure(conn, "CN.688041", "2025FY") is None


@pytest.mark.db
def test_half_year_report_is_not_an_annual_report(conn: psycopg.Connection) -> None:
    _ann(conn, "海光信息2024年半年度报告", "2024-08-20")
    assert first_disclosure(conn, "CN.688041", "2024FY") is None
    assert first_disclosure(conn, "CN.688041", "2024H1") is not None


@pytest.mark.db
def test_english_and_cancelled_versions_are_ignored(conn: psycopg.Connection) -> None:
    _ann(conn, "海光信息2024年年度报告(英文版)", "2025-03-20")
    _ann(conn, "关于取消2024年年度报告的公告", "2025-03-21")
    assert first_disclosure(conn, "CN.688041", "2024FY") is None


@pytest.mark.db
def test_only_primary_sources_count_even_when_superseded(conn: psycopg.Connection) -> None:
    _ann(conn, "海光信息：2024年年度报告", "2025-03-25", source="ifind")
    assert first_disclosure(conn, "CN.688041", "2024FY") is None
    _ann(conn, "海光信息2024年年度报告", "2025-03-28", superseded=True)
    d = first_disclosure(conn, "CN.688041", "2024FY")
    assert d is not None and d.known_at.date() == date(2025, 3, 28)


@pytest.mark.db
def test_prefixed_full_company_name_and_quarters(conn: psycopg.Connection) -> None:
    _ann(conn, "海光信息技术股份有限公司2025年第三季度报告", "2025-10-25")
    _ann(conn, "海光信息技术股份有限公司2025年第一季度报告", "2025-04-26")
    q3 = first_disclosure(conn, "CN.688041", "2025Q3")
    q1 = first_disclosure(conn, "CN.688041", "2025Q1")
    assert q3 is not None and q3.known_at.date() == date(2025, 10, 25)
    assert q1 is not None and q1.known_at.date() == date(2025, 4, 26)


@pytest.mark.db
def test_publication_far_from_period_end_is_rejected(conn: psycopg.Connection) -> None:
    """往年同名报告的重发/转载不能被算作这一期的披露。"""
    _ann(conn, "海光信息2024年年度报告", "2027-01-05")
    assert first_disclosure(conn, "CN.688041", "2024FY") is None


@pytest.mark.db
def test_missing_returns_none(conn: psycopg.Connection) -> None:
    assert first_disclosure(conn, "CN.688041", "2024FY") is None
