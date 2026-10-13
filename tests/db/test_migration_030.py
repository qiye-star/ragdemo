"""迁移 030：core.doc_table 是登记在册的时点表，有 asof 视图、RLS，时点列不变量成立。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.migrate import migrate
from ragdemo_core.db.session import as_of_session

MIGRATIONS = Path("db/migrations")


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(temp_db) as c:
        migrate(c, MIGRATIONS)
        c.execute(
            "INSERT INTO core.document (doc_id, doc_type, title, publish_at, source, content_hash,"
            " version_group_id, valid_from, known_at, ingest_run_id) OVERRIDING SYSTEM VALUE"
            " VALUES (1, 'quarterly', 't', '2025-10-28 18:00+08', 'mock', 'h', 1, '2025-10-28',"
            " '2025-10-28 18:00+08', 'r1')"
        )
        c.execute(
            "INSERT INTO core.doc_block (block_id, doc_id, block_type, ordinal, page, content,"
            " is_leaf, doc_type, publish_at, valid_from, known_at, source, ingest_run_id)"
            " OVERRIDING SYSTEM VALUE"
            " VALUES (10, 1, 'table', 0, 3, '| a |', true, 'quarterly', '2025-10-28 18:00+08',"
            " '2025-10-28', '2025-10-28 18:00+08', 'mock', 'r1')"
        )
        c.commit()
        yield c


GRID_JSON = (
    '{"v":1,"engine":"textin:t","ref":null,"hr":0,"hc":0,"cells":'
    '[{"r":0,"c":0,"rs":1,"cs":1,"t":"7","p":3,"b":null,"h":false,"k":"not_checked"}]}'
)

_INSERT_COLS = (
    "INSERT INTO core.doc_table (block_id, doc_id, grid, grid_version, n_rows, n_cols,"
    " header_rows, header_cols, pages, unit_source, statement_kind, source_engine,"
    " verify_version, verify_verdict, cells_numeric, cells_match, cells_mismatch,"
    " cells_unverifiable, doc_type, publish_at, can_show_raw, valid_from, known_at, source,"
    " ingest_run_id)"
)


def _insert(
    c: psycopg.Connection, *, known_at: str = "2025-10-28 18:00+08", verdict: str = "not_checked"
) -> None:
    c.execute(
        _INSERT_COLS
        + " VALUES (10, 1, %s::jsonb, 'tablegrid:v1', 1, 1, 0, 0, '{3}', 'none', 'other',"
        " 'textin:t',"
        " 'cellverify:v3', %s, 1, 0, 0, 1, 'quarterly', '2025-10-28 18:00+08', true,"
        " '2025-10-28', %s, 'mock', 'r1')",
        (GRID_JSON, verdict, known_at),
    )


@pytest.mark.db
def test_registered_with_view_and_invariants_hold(conn: psycopg.Connection) -> None:
    _insert(conn)
    conn.commit()
    assert check_schema_invariants(conn) == []
    with as_of_session(conn, datetime(2025, 10, 27, tzinfo=UTC)) as s:
        assert s.execute("SELECT count(*) FROM asof.doc_table").fetchone() == (0,)
    with as_of_session(conn, datetime(2025, 10, 29, tzinfo=UTC)) as s:
        assert s.execute("SELECT count(*) FROM asof.doc_table").fetchone() == (1,)


@pytest.mark.db
def test_known_at_drift_from_block_is_an_invariant_violation(conn: psycopg.Connection) -> None:
    _insert(conn, known_at="2025-10-29 00:00+08")
    conn.commit()
    assert any("doc_table" in v for v in check_schema_invariants(conn))


@pytest.mark.db
def test_live_table_on_superseded_block_is_an_invariant_violation(conn: psycopg.Connection) -> None:
    _insert(conn)
    conn.execute("UPDATE core.doc_block SET superseded_at = now() WHERE block_id = 10")
    conn.commit()
    assert any("doc_table" in v for v in check_schema_invariants(conn))


@pytest.mark.db
def test_only_one_live_grid_per_block(conn: psycopg.Connection) -> None:
    _insert(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(conn)


@pytest.mark.db
def test_superseded_grid_allows_a_new_live_one(conn: psycopg.Connection) -> None:
    _insert(conn)
    conn.execute("UPDATE core.doc_table SET superseded_at = now()")
    _insert(conn)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM core.doc_table").fetchone() == (2,)


@pytest.mark.db
def test_cell_counts_cannot_exceed_numeric(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            _INSERT_COLS
            + " VALUES (10, 1, '{}', 'g', 1, 1, 0, 0, '{3}', 'none', 'other', 'e', 'v',"
            " 'not_checked',"
            " 0, 1, 0, 0, 'quarterly', '2025-10-28 18:00+08', true, '2025-10-28',"
            " '2025-10-28 18:00+08', 'mock', 'r1')"
        )


@pytest.mark.db
def test_verdict_check_accepts_partial_and_rejects_unknown(conn: psycopg.Connection) -> None:
    _insert(conn, verdict="text_layer_cells_partial")
    conn.execute("UPDATE core.doc_table SET superseded_at = now()")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(conn, verdict="verified_by_magic")


@pytest.mark.db
def test_row_units_column_exists_and_defaults_empty(conn: psycopg.Connection) -> None:
    _insert(conn)
    assert conn.execute("SELECT row_units, column_units FROM core.doc_table").fetchone() == ({}, {})


@pytest.mark.db
@pytest.mark.parametrize(
    "assignment",
    [
        "n_rows = 2",
        "n_cols = 4",
        "header_rows = 1",
        "header_cols = 1",
        "pages = '{4}'",
        "cells_match = 1, cells_unverifiable = 0",
        "cells_mismatch = 1, cells_unverifiable = 0",
        "verify_verdict = 'text_layer_cells_mismatch'",
        "verify_verdict = 'text_layer_cells_ok'",
        "valid_from = '2025-10-29'",
        "source = 'other'",
        "source_engine = 'textin:x'",
        "provider_ref = 'x'",
    ],
)
def test_denormalised_metadata_drift_from_grid_json_is_a_violation(
    conn: psycopg.Connection, assignment: str
) -> None:
    _insert(conn)
    conn.commit()
    assert check_schema_invariants(conn) == []
    conn.execute(f"UPDATE core.doc_table SET {assignment}")
    conn.commit()
    assert any("doc_table" in v for v in check_schema_invariants(conn)), assignment


@pytest.mark.db
def test_grid_without_cells_is_a_violation(conn: psycopg.Connection) -> None:
    _insert(conn)
    conn.execute("UPDATE core.doc_table SET grid = '{\"v\":1}'::jsonb")
    conn.commit()
    assert any("doc_table" in v for v in check_schema_invariants(conn))
