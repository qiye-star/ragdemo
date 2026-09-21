"""容器与扩展的连通性自检。"""

from __future__ import annotations

import psycopg
import pytest

REQUIRED_EXTENSIONS = {"pg_search", "vector", "pgcrypto", "pg_trgm"}


@pytest.mark.db
def test_required_extensions_are_available(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM pg_available_extensions WHERE name = ANY(%s)",
            (sorted(REQUIRED_EXTENSIONS),),
        ).fetchall()
    assert {r[0] for r in rows} == REQUIRED_EXTENSIONS


@pytest.mark.db
def test_server_is_postgres_15_or_newer(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    assert int(row[0]) >= 150000
