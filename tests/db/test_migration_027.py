"""迁移 027：券商研报来源登记（ADR-0014）——可检索、不展示原文。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.documents import DocumentWriter
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        yield c


@pytest.mark.db
def test_research_source_is_registered_as_searchable_but_not_showable(
    conn: psycopg.Connection,
) -> None:
    row = conn.execute(
        "SELECT can_cache, can_show_raw, can_vectorize, time_precision"
        " FROM core.source_registry WHERE source_id = 'eastmoney-research'"
    ).fetchone()
    assert row == (True, False, True, "day")


@pytest.mark.db
def test_document_writer_for_research_inherits_can_show_raw_false(
    conn: psycopg.Connection,
) -> None:
    writer = DocumentWriter(conn, ingest_run_id="r1", source="eastmoney-research")
    assert writer.can_show_raw is False
    # 公告来源不受影响
    assert DocumentWriter(conn, ingest_run_id="r1", source="cninfo").can_show_raw is True
