"""tables / retrieval 的 db 测试共用的网格夹具助手（T9/T14/T18）。"""

from __future__ import annotations

from dataclasses import replace

import psycopg

from ragdemo.tables.context import TableContext
from ragdemo.tables.grid import BBox, GridCell, TableGrid
from ragdemo.tables.statements import statement_kind
from ragdemo.tables.store import TableRecord, insert_table
from ragdemo.tables.verify_cells import VERIFY_CELLS_VERSION, TableVerdict, summarize_grid

_BOX: BBox = (0.1, 0.1, 0.5, 0.2)


def insert_test_grid(
    conn: psycopg.Connection,
    *,
    unit: str | None = "万元",
    caption: str | None = "分部收入",
    verdict: TableVerdict = "text_layer_cells_ok",
    currency: str | None = None,
) -> int:
    """给 corpus 里唯一的表格块写入一版网格，返回该块的 block_id。已提交。"""
    row = conn.execute("SELECT block_id FROM core.doc_block WHERE block_type = 'table'").fetchone()
    assert row is not None
    block_id = int(row[0])
    grid = TableGrid(
        cells=(
            GridCell(0, 0, 1, 1, "业务分部", page=1, bbox=_BOX, is_header=True),
            GridCell(0, 1, 1, 1, "收入", page=1, bbox=_BOX, is_header=True),
            GridCell(1, 0, 1, 1, "智能计算", page=1, bbox=_BOX),
            GridCell(1, 1, 1, 1, "12,340", page=1, bbox=_BOX),
        ),
        source_engine="textin:t",
        provider_ref="p1",
        header_rows=1,
        header_cols=1,
    )
    ctx = TableContext(
        caption=caption,
        unit=unit,
        unit_source="preceding_block" if unit else "none",
        currency=currency,
    )
    summary = replace(summarize_grid(grid), verdict=verdict)
    rec = TableRecord(
        block_id=block_id,
        grid=grid,
        context=ctx,
        summary=summary,
        statement_kind=statement_kind(grid),
        verify_version=VERIFY_CELLS_VERSION,
        desc_model=None,
        desc_prompt_version=None,
    )
    insert_table(conn, rec, ingest_run_id="t14")
    conn.commit()
    return block_id
