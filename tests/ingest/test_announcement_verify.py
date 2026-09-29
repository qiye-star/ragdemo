"""公告对账：来源自报总数 vs 库里现行行数。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from ragdemo.ingest.announcement_verify import verify_announcements
from ragdemo.ingest.announcements import AnnouncementSources, EntityRoute
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.migrate import migrate
from tests.ingest.test_announcements import CN_ANNOUNCEMENT, _cninfo_deps, _cninfo_ok

MIGRATIONS = Path("db/migrations")
ROUTE = EntityRoute(entity_ref="688041.SH", short_name="海光信息", market="SSE")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    yield c
    c.close()


def _insert(conn: psycopg.Connection) -> None:
    _entity(conn)
    conn.execute(
        "INSERT INTO core.announcement_index (entity_id, source, source_ref, title, title_norm,"
        " publish_date, valid_from, known_at, has_full_text, ingest_run_id) VALUES"
        " ('CN.688041','cninfo','1','t','t','2026-01-01','2026-01-01','2026-01-01T15:59:59Z',"
        "false,'x')"
    )
    conn.commit()


def _entity(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, market, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) VALUES"
        " ('CN.688041', '海光信息', 'listed', 'SSE', '算力', 'AI芯片', ARRAY['通用服务器CPU'],"
        "  '通用服务器CPU', '688041.SH')"
    )


@pytest.mark.db
def test_matching_counts_pass(conn: psycopg.Connection, tmp_path: Path) -> None:
    _insert(conn)
    deps = _cninfo_deps(_cninfo_ok([CN_ANNOUNCEMENT]), LocalBlobStore(tmp_path))
    (row,) = verify_announcements(
        conn,
        [ROUTE],
        AnnouncementSources(cninfo=deps),
        since=date(2025, 1, 1),
        until=date(2026, 9, 28),
    )
    assert row.ok and row.source_total == 1 and row.db_count == 1


@pytest.mark.db
def test_missing_row_is_reported(conn: psycopg.Connection, tmp_path: Path) -> None:
    _entity(conn)
    conn.commit()
    deps = _cninfo_deps(_cninfo_ok([CN_ANNOUNCEMENT]), LocalBlobStore(tmp_path))
    (row,) = verify_announcements(
        conn,
        [ROUTE],
        AnnouncementSources(cninfo=deps),
        since=date(2025, 1, 1),
        until=date(2026, 9, 28),
    )
    assert not row.ok and row.db_count == 0 and "≠" in row.note
