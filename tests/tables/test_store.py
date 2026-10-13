"""core.doc_table 的读写：只经 asof 视图读，时点与 RLS 继承自所属块。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ragdemo.tables.context import TableContext, bind_context
from ragdemo.tables.grid import BBox, GridCell, TableGrid
from ragdemo.tables.statements import statement_kind
from ragdemo.tables.store import (
    TableRecord,
    insert_table,
    live_table_meta,
    load_table,
    supersede_table,
    supersede_tables_of_doc,
)
from ragdemo.tables.verify_cells import VERIFY_CELLS_VERSION, summarize_grid
from ragdemo_core.db.invariants import check_schema_invariants

BOX: BBox = (0.1, 0.1, 0.5, 0.2)


def _grid(page: int | None = 1) -> TableGrid:
    return TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "业务分部", page=page, bbox=BOX, is_header=True),
            GridCell(0, 1, 1, 1, "收入(万元)", page=page, bbox=BOX, is_header=True),
            GridCell(1, 0, 1, 1, "智能计算", page=page, bbox=BOX),
            GridCell(1, 1, 1, 1, "12,340", page=page, bbox=BOX),
        ),
        source_engine="textin:t",
        provider_ref="p1",
        header_rows=1,
        header_cols=1,
    )


def _record(
    block_id: int, grid: TableGrid | None = None, ctx: TableContext | None = None
) -> TableRecord:
    g = grid or _grid()
    return TableRecord(
        block_id=block_id,
        grid=g,
        context=ctx or bind_context(g, caption="分部收入", preceding_texts=["单位：万元"]),
        summary=summarize_grid(g),
        statement_kind=statement_kind(g),
        verify_version=VERIFY_CELLS_VERSION,
        desc_model=None,
        desc_prompt_version=None,
    )


def _table_block(conn: psycopg.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT block_id, doc_id FROM core.doc_block WHERE block_type = 'table' AND doc_id = 1"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


@pytest.fixture
def conn(corpus: psycopg.Connection) -> psycopg.Connection:
    return corpus


@pytest.mark.db
def test_insert_then_load_round_trips(conn: psycopg.Connection, as_of_2024: datetime) -> None:
    block_id, doc_id = _table_block(conn)
    g = _grid()
    table_id = insert_table(conn, _record(block_id, g), ingest_run_id="r9")
    conn.commit()
    got = load_table(conn, block_id, as_of=as_of_2024)
    assert got is not None
    assert (got.table_id, got.block_id, got.doc_id) == (table_id, block_id, doc_id)
    assert got.grid == g
    assert got.pages == (1,)
    assert got.unit == "万元"
    assert got.caption == "分部收入"
    assert got.currency is None
    assert got.can_show_raw is True
    row = conn.execute(
        "SELECT known_at FROM core.doc_block WHERE block_id = %s", (block_id,)
    ).fetchone()
    assert row is not None
    assert got.known_at == row[0]
    assert check_schema_invariants(conn) == []


@pytest.mark.db
def test_row_and_column_units_round_trip_sorted(
    conn: psycopg.Connection, as_of_2024: datetime
) -> None:
    block_id, _ = _table_block(conn)
    ctx = TableContext(
        caption=None,
        unit=None,
        unit_source="none",
        currency="USD",
        column_units=((10, "%"), (2, "万元")),
        row_units=((7, "万元"), (3, "%")),
    )
    insert_table(conn, _record(block_id, ctx=ctx), ingest_run_id="r9")
    conn.commit()
    got = load_table(conn, block_id, as_of=as_of_2024)
    assert got is not None
    assert got.column_units == ((2, "万元"), (10, "%"))
    assert got.row_units == ((3, "%"), (7, "万元"))
    assert got.currency == "USD"


@pytest.mark.db
def test_as_of_before_block_known_at_returns_none(conn: psycopg.Connection) -> None:
    block_id, _ = _table_block(conn)
    insert_table(conn, _record(block_id), ingest_run_id="r9")
    conn.commit()
    assert load_table(conn, block_id, as_of=datetime(2024, 10, 1, tzinfo=UTC)) is None


@pytest.mark.db
def test_unlocated_cells_fall_back_to_block_page(
    conn: psycopg.Connection, as_of_2024: datetime
) -> None:
    """多页表的格可能 page=None；没有任何格带页码时，pages 取所属块的页码。"""
    block_id, _ = _table_block(conn)
    g = TableGrid(cells=(GridCell(0, 0, 1, 1, "7"),), source_engine="x")
    insert_table(conn, _record(block_id, g), ingest_run_id="r9")
    conn.commit()
    got = load_table(conn, block_id, as_of=as_of_2024)
    assert got is not None
    assert got.pages == (1,)
    assert got.grid == g


@pytest.mark.db
def test_insert_for_non_table_or_missing_block_raises(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'paragraph' LIMIT 1"
    ).fetchone()
    assert row is not None
    with pytest.raises(ValueError, match="不是现行表格块"):
        insert_table(conn, _record(int(row[0])), ingest_run_id="r9")
    with pytest.raises(ValueError, match="不是现行表格块"):
        insert_table(conn, _record(999999), ingest_run_id="r9")


@pytest.mark.db
def test_second_live_grid_for_same_block_is_rejected(conn: psycopg.Connection) -> None:
    block_id, _ = _table_block(conn)
    insert_table(conn, _record(block_id), ingest_run_id="r9")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_table(conn, _record(block_id), ingest_run_id="r9")


@pytest.mark.db
def test_supersede_table_then_insert_new_version(
    conn: psycopg.Connection, as_of_2024: datetime
) -> None:
    block_id, _ = _table_block(conn)
    old = insert_table(conn, _record(block_id), ingest_run_id="r9")
    conn.commit()
    meta = live_table_meta(conn, block_id)
    assert meta is not None
    assert meta[0] == old
    assert meta[1:] == ("tablegrid:v1", VERIFY_CELLS_VERSION, "not_checked")
    supersede_table(conn, old)
    assert live_table_meta(conn, block_id) is None
    new = insert_table(conn, _record(block_id), ingest_run_id="r10")
    conn.commit()
    got = load_table(conn, block_id, as_of=datetime.now(UTC) + timedelta(seconds=5))
    assert got is not None
    assert got.table_id == new != old


@pytest.mark.db
def test_supersede_tables_of_doc(conn: psycopg.Connection, as_of_2024: datetime) -> None:
    block_id, doc_id = _table_block(conn)
    insert_table(conn, _record(block_id), ingest_run_id="r9")
    assert supersede_tables_of_doc(conn, doc_id) == 1
    assert supersede_tables_of_doc(conn, doc_id) == 0  # 幂等
    conn.commit()
    # 取代发生在"现在"：当下读不到；但在取代之前的时点（as_of_2024）这版网格仍然有效。
    assert load_table(conn, block_id, as_of=datetime.now(UTC) + timedelta(seconds=5)) is None
    assert load_table(conn, block_id, as_of=as_of_2024) is not None
    assert live_table_meta(conn, block_id) is None


@pytest.mark.db
def test_private_block_is_invisible_without_matching_user(
    conn: psycopg.Connection, temp_db: str, as_of_2024: datetime
) -> None:
    block_id, doc_id = _table_block(conn)
    insert_table(conn, _record(block_id), ingest_run_id="r9")
    for table in ("document", "doc_block", "doc_table"):
        conn.execute(f"UPDATE core.{table} SET owner_user = 'u1' WHERE doc_id = %s", (doc_id,))
    reader = f"apptest_{uuid.uuid4().hex[:10]}"
    conn.execute(f"""CREATE USER "{reader}" PASSWORD 'not-a-real-secret'""")
    conn.execute(f'GRANT app_read TO "{reader}"')
    conn.commit()
    try:
        with psycopg.connect(temp_db, user=reader, password="not-a-real-secret") as c:
            assert load_table(c, block_id, as_of=as_of_2024) is None
            got = load_table(c, block_id, as_of=as_of_2024, user="u1")
            assert got is not None
            assert got.block_id == block_id
    finally:
        conn.rollback()
        conn.execute(f'DROP OWNED BY "{reader}"')
        conn.execute(f'DROP USER IF EXISTS "{reader}"')
        conn.commit()
