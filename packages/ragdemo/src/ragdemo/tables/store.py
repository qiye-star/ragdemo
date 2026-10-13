"""`core.doc_table` 的读写（表格处理改进计划 T8）。

写：反规范化列（doc_id / entity_id / doc_type / known_at / publish_at / owner_* / can_show_raw /
valid_from / source）一律从所属 `core.doc_block` 读回，调用方不能传——单一数据源，与
`DocumentWriter._insert_blocks` 对 `core.document` 的做法一致。时点列因此继承块的 `known_at`
（公告的发布时刻，不是入库时间）。写入方法都不提交，事务边界属于调用方。

读：只经 `asof.doc_table`（`as_of_session` 内），应用层不得直接查基表。`live_table_meta`
是唯一读 `core` 的函数——写入方与回填的幂等判断需要看"当前 live 的那行"，与 as_of 无关。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from ragdemo.tables.context import TableContext
from ragdemo.tables.grid import GRID_VERSION, TableGrid, grid_from_json, grid_to_json
from ragdemo.tables.statements import StatementKind
from ragdemo.tables.verify_cells import GridVerifySummary
from ragdemo_core.db.session import as_of_session


@dataclass(frozen=True)
class TableRecord:
    block_id: int
    grid: TableGrid
    context: TableContext
    summary: GridVerifySummary
    statement_kind: StatementKind
    verify_version: str
    desc_model: str | None
    desc_prompt_version: str | None
    continues_from_block_id: int | None = None


@dataclass(frozen=True)
class StoredTable:
    table_id: int
    block_id: int
    doc_id: int
    grid: TableGrid
    pages: tuple[int, ...]
    caption: str | None
    unit: str | None
    column_units: tuple[tuple[int, str], ...]
    row_units: tuple[tuple[int, str], ...]
    currency: str | None
    statement_kind: str
    verify_verdict: str
    can_show_raw: bool
    known_at: datetime


_INSERT = """
INSERT INTO core.doc_table (
  block_id, doc_id, grid, grid_version, n_rows, n_cols, header_rows, header_cols, pages,
  caption, unit, unit_source, column_units, row_units, currency, statement_kind, source_engine,
  provider_ref, verify_version, verify_verdict, cells_numeric, cells_match, cells_mismatch,
  cells_unverifiable, desc_model, desc_prompt_version, continues_from_block_id,
  entity_id, doc_type, publish_at, owner_tenant, owner_user, can_show_raw,
  valid_from, known_at, source, source_ref, ingest_run_id)
SELECT b.block_id, b.doc_id, %(grid)s, %(grid_version)s, %(n_rows)s, %(n_cols)s, %(hr)s, %(hc)s,
       COALESCE(NULLIF(%(pages)s::int[], '{}'::int[]),
                CASE WHEN b.page IS NULL THEN NULL ELSE ARRAY[b.page] END),
       %(caption)s, %(unit)s, %(unit_source)s, %(column_units)s, %(row_units)s, %(currency)s,
       %(kind)s, %(engine)s, %(ref)s,
       %(verify_version)s, %(verdict)s, %(numeric)s, %(match)s, %(mismatch)s, %(unverifiable)s,
       %(desc_model)s, %(desc_prompt)s, %(continues)s,
       b.entity_id, b.doc_type, b.publish_at, b.owner_tenant, b.owner_user, b.can_show_raw,
       b.valid_from, b.known_at, b.source, b.source_ref, %(run_id)s
  FROM core.doc_block b
 WHERE b.block_id = %(block_id)s AND b.block_type = 'table' AND b.superseded_at IS NULL
RETURNING table_id
"""

_SELECT = """
SELECT table_id, block_id, doc_id, grid, pages, caption, unit, column_units, row_units, currency,
       statement_kind, verify_verdict, can_show_raw, known_at
  FROM asof.doc_table WHERE block_id = %(block_id)s
"""


def _units_json(units: tuple[tuple[int, str], ...]) -> Jsonb:
    return Jsonb({str(i): u for i, u in units})


def _units_from_json(raw: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(raw, Mapping):
        return ()
    return tuple(sorted((int(k), str(v)) for k, v in raw.items()))


def insert_table(conn: psycopg.Connection[Any], rec: TableRecord, *, ingest_run_id: str) -> int:
    """插入一版网格，返回 table_id。不提交。

    找不到现行表格块 -> `ValueError`。同一块已有 live 网格 -> `UniqueViolation`（先
    `supersede_table`）。网格没有任何带页码的格（跨页表的未定位格）时，页码取块的页码。
    """
    g, ctx, s = rec.grid, rec.context, rec.summary
    row = conn.execute(
        _INSERT,
        {
            "grid": Jsonb(grid_to_json(g)),
            "grid_version": GRID_VERSION,
            "n_rows": g.n_rows,
            "n_cols": g.n_cols,
            "hr": g.header_rows,
            "hc": g.header_cols,
            "pages": list(g.pages),
            "caption": ctx.caption,
            "unit": ctx.unit,
            "unit_source": ctx.unit_source,
            "column_units": _units_json(ctx.column_units),
            "row_units": _units_json(ctx.row_units),
            "currency": ctx.currency,
            "kind": rec.statement_kind,
            "engine": g.source_engine,
            "ref": g.provider_ref,
            "verify_version": rec.verify_version,
            "verdict": s.verdict,
            "numeric": s.numeric,
            "match": s.match,
            "mismatch": s.mismatch,
            "unverifiable": s.unverifiable,
            "desc_model": rec.desc_model,
            "desc_prompt": rec.desc_prompt_version,
            "continues": rec.continues_from_block_id,
            "run_id": ingest_run_id,
            "block_id": rec.block_id,
        },
    ).fetchone()
    if row is None:
        raise ValueError(f"块 {rec.block_id} 不是现行表格块")
    return int(row[0])


def live_table_meta(
    conn: psycopg.Connection[Any], block_id: int
) -> tuple[int, str, str, str] | None:
    """`(table_id, grid_version, verify_version, verify_verdict)`。

    读 `core`（不经 as_of）：写入方与回填判幂等时要看"当前 live 的那行"。
    """
    row = conn.execute(
        "SELECT table_id, grid_version, verify_version, verify_verdict FROM core.doc_table"
        " WHERE block_id = %s AND superseded_at IS NULL",
        (block_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), str(row[2]), str(row[3])


def supersede_table(conn: psycopg.Connection[Any], table_id: int) -> None:
    """把一行网格标为被取代（已被取代的行不动）。不提交。"""
    conn.execute(
        "UPDATE core.doc_table SET superseded_at = now() WHERE table_id = %s"
        " AND superseded_at IS NULL",
        (table_id,),
    )


def supersede_tables_of_doc(conn: psycopg.Connection[Any], doc_id: int) -> int:
    """把一份文档下全部 live 网格标为被取代，返回行数。不提交。

    与 `UPDATE core.doc_block SET superseded_at = now()` 同事务调用，两处 `now()` 相同。
    """
    cur = conn.execute(
        "UPDATE core.doc_table SET superseded_at = now() WHERE doc_id = %s"
        " AND superseded_at IS NULL",
        (doc_id,),
    )
    return cur.rowcount


def load_table(
    conn: psycopg.Connection[Any],
    block_id: int,
    *,
    as_of: datetime,
    tenant: str | None = None,
    user: str | None = None,
) -> StoredTable | None:
    with as_of_session(conn, as_of, tenant=tenant, user=user) as s:
        row = s.execute(_SELECT, {"block_id": block_id}).fetchone()
    if row is None:
        return None
    return StoredTable(
        table_id=cast(int, row[0]),
        block_id=cast(int, row[1]),
        doc_id=cast(int, row[2]),
        grid=grid_from_json(cast("Mapping[str, Any]", row[3])),
        pages=tuple(int(p) for p in cast("Iterable[int]", row[4])),
        caption=cast("str | None", row[5]),
        unit=cast("str | None", row[6]),
        column_units=_units_from_json(row[7]),
        row_units=_units_from_json(row[8]),
        currency=cast("str | None", row[9]),
        statement_kind=str(row[10]),
        verify_verdict=str(row[11]),
        can_show_raw=bool(row[12]),
        known_at=cast(datetime, row[13]),
    )


__all__ = [
    "StoredTable",
    "TableRecord",
    "insert_table",
    "live_table_meta",
    "load_table",
    "supersede_table",
    "supersede_tables_of_doc",
]
