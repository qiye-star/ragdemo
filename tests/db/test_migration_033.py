"""迁移 033：doc_block.parse_source / figure_meta 两列、两条 CHECK、asof 视图可见。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        yield c


@pytest.mark.db
def test_asof_view_exposes_both_columns(conn: psycopg.Connection) -> None:
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'asof' AND table_name = 'doc_block'"
        )
    }
    assert {"parse_source", "figure_meta"} <= cols


@pytest.mark.db
def test_asof_view_keeps_the_standard_predicate(conn: psycopg.Connection) -> None:
    definition = str(
        conn.execute("SELECT pg_get_viewdef('asof.doc_block'::regclass)").fetchone()[0]  # type: ignore[index]
    )
    assert "known_at <= asof.current_as_of()" in definition
    assert "superseded_at IS NULL" in definition
    assert "superseded_at > asof.current_as_of()" in definition


@pytest.mark.db
def test_both_checks_exist(conn: psycopg.Connection) -> None:
    defs = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'core.doc_block'::regclass"
        )
    }
    assert (
        "'local'" in defs["doc_block_parse_source_known"]
        and "'mixed'" in defs["doc_block_parse_source_known"]
    )
    assert "figure" in defs["doc_block_figure_meta_only_figure"]
