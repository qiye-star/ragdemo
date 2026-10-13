"""迁移 031：core.fact_table_ref + 事实 ↔ 单元格校验的端到端（不改任何事实）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import psycopg
import pytest
from click.testing import CliRunner

from ragdemo.ingest.facts_cli import facts_group
from ragdemo.tables import fact_check
from ragdemo.tables.context import TableContext
from ragdemo.tables.fact_check import run_fact_table_check
from ragdemo.tables.grid import BBox, GridCell, TableGrid
from ragdemo.tables.headers import infer_headers
from ragdemo.tables.statements import statement_kind
from ragdemo.tables.store import TableRecord, insert_table
from ragdemo.tables.verify_cells import VERIFY_CELLS_VERSION, summarize_grid
from ragdemo_core.db.invariants import check_schema_invariants
from ragdemo_core.db.session import as_of_session

BOX: BBox = (0.1, 0.1, 0.5, 0.2)
FACT_KNOWN = "2024-10-28 18:32+08"


@pytest.fixture
def conn(corpus: psycopg.Connection) -> psycopg.Connection:
    corpus.execute(
        "INSERT INTO core.node_metric (metric_id, metric_name, metric_role, frequency,"
        " source_type, definition, unit) VALUES ('revenue_total','营业收入','confirming',"
        "'quarterly','filing','合并报表营业收入','CNY')"
    )
    # 标题须能解析出报告期（own_doc = 文档期别与事实期别相同）
    corpus.execute("UPDATE core.document SET title = '寒武纪2024年第三季度报告' WHERE doc_id = 1")
    corpus.commit()
    return corpus


def _add_fact(c: psycopg.Connection, *, value: str, period: str = "2024Q3") -> int:
    row = c.execute(
        "INSERT INTO core.fin_fact (entity_id, metric_id, period, period_end, value, unit,"
        " valid_from, known_at, source, ingest_run_id) VALUES ('CN.688256','revenue_total',"
        " %s, '2024-09-30', %s, 'CNY', '2024-07-01', %s, 'wind', 'r1') RETURNING fact_id",
        (period, value, FACT_KNOWN),
    ).fetchone()
    assert row is not None
    c.commit()
    return int(row[0])


def _add_table(c: psycopg.Connection, *, number: str = "12,340.00") -> tuple[int, int]:
    """给 doc 1 的表格块写一版网格：2024年1-9月 营业收入（万元）。返回 (table_id, block_id)。"""
    row = c.execute(
        "SELECT block_id FROM core.doc_block WHERE block_type = 'table' AND doc_id = 1"
    ).fetchone()
    assert row is not None
    block_id = int(row[0])
    grid = infer_headers(
        TableGrid(
            cells=(
                GridCell(0, 0, 1, 1, "项目", page=1),
                GridCell(0, 1, 1, 1, "2024年1-9月", page=1),
                GridCell(1, 0, 1, 1, "营业收入", page=1, bbox=BOX, check="no_numbers"),
                GridCell(1, 1, 1, 1, number, page=1, bbox=BOX, check="match"),
            ),
            source_engine="textin:t",
            provider_ref="p1",
        )
    )
    rec = TableRecord(
        block_id=block_id,
        grid=grid,
        context=TableContext("合并利润表", "万元", "in_table", "CNY"),
        summary=summarize_grid(grid),
        statement_kind=statement_kind(grid),
        verify_version=VERIFY_CELLS_VERSION,
        desc_model=None,
        desc_prompt_version=None,
    )
    table_id = insert_table(c, rec, ingest_run_id="t18")
    c.commit()
    return table_id, block_id


def _refs(c: psycopg.Connection) -> list[tuple[object, ...]]:
    return c.execute(
        "SELECT fact_id, table_id, cell_row, cell_col, verdict, report_value, fact_value,"
        " check_version, superseded_at IS NULL FROM core.fact_table_ref ORDER BY ref_id"
    ).fetchall()


def _fact_digest(c: psycopg.Connection) -> str:
    row = c.execute(
        "SELECT md5(string_agg(t::text, ',' ORDER BY fact_id)) FROM core.fin_fact t"
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.db
def test_verdict_is_arithmetic_and_cannot_be_forged(conn: psycopg.Connection) -> None:
    fact_id = _add_fact(conn, value="123400000")
    table_id, block_id = _add_table(conn)
    cols = (
        "INSERT INTO core.fact_table_ref (fact_id, table_id, block_id, cell_row, cell_col, page,"
        " entity_id, metric_id, period, report_value, fact_value, tolerance, unit_scale,"
        " period_label, verdict, check_version, valid_from, known_at, source, ingest_run_id)"
        " VALUES (%s, %s, %s, 1, 1, 1, 'CN.688256', 'revenue_total', '2024Q3', %s, 123400000,"
        " 6000, 10000, 'x', %s, 'v', '2024-07-01', %s, 'table_check', 'r')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(cols, (fact_id, table_id, block_id, 123500000, "match", FACT_KNOWN))
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(cols, (fact_id, table_id, block_id, 123400000, "mismatch", FACT_KNOWN))
    conn.rollback()
    conn.execute(cols, (fact_id, table_id, block_id, 123500000, "mismatch", FACT_KNOWN))
    conn.execute(
        cols.replace("1, 1, 1,", "2, 2, 1,"),
        (fact_id, table_id, block_id, 123405999, "match", FACT_KNOWN),
    )
    conn.commit()


@pytest.mark.db
def test_registered_view_hides_future_and_invariants_hold(conn: psycopg.Connection) -> None:
    _add_fact(conn, value="123400000")
    _add_table(conn)
    run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert check_schema_invariants(conn) == []
    with as_of_session(conn, datetime(2024, 10, 27, tzinfo=UTC)) as s:
        assert s.execute("SELECT count(*) FROM asof.fact_table_ref").fetchone() == (0,)
    with as_of_session(conn, datetime(2024, 12, 31, tzinfo=UTC)) as s:
        assert s.execute("SELECT count(*) FROM asof.fact_table_ref").fetchone() == (1,)


@pytest.mark.db
def test_end_to_end_idempotent_versioned_and_never_touches_facts(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fact_id = _add_fact(conn, value="123400000")
    table_id, block_id = _add_table(conn)
    before = _fact_digest(conn)

    dry = run_fact_table_check(conn, ingest_run_id="t", write=False)
    assert dry.counts() == {"match": 1} and dry.written == 0
    assert _refs(conn) == []

    first = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert (first.written, first.superseded) == (1, 0)
    rows = _refs(conn)
    assert len(rows) == 1
    assert rows[0][:5] == (fact_id, table_id, 1, 1, "match")
    src = conn.execute(
        "SELECT source_ref, page, period_label, known_at FROM core.fact_table_ref"
    ).fetchone()
    assert src is not None and src[0] == f"{block_id}:r1c1" and src[1] == 1

    again = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert (again.written, again.superseded) == (0, 0)

    monkeypatch.setattr(fact_check, "FACT_CHECK_VERSION", "factcell:v2")
    v2 = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert (v2.written, v2.superseded) == (1, 1)
    live = conn.execute(
        "SELECT check_version FROM core.fact_table_ref WHERE superseded_at IS NULL"
    ).fetchall()
    assert live == [("factcell:v2",)]
    assert conn.execute("SELECT count(*) FROM core.fact_table_ref").fetchone() == (2,)

    assert _fact_digest(conn) == before
    assert conn.execute("SELECT source_block FROM core.fin_fact").fetchone() == (None,)


@pytest.mark.db
def test_mismatch_is_recorded_and_flips_when_the_numbers_change(conn: psycopg.Connection) -> None:
    _add_fact(conn, value="999999999")
    _add_table(conn)
    report = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert report.counts() == {"mismatch": 1}
    assert [r[4] for r in _refs(conn)] == ["mismatch"]
    # 同一格、事实值变了 → 结论变化，旧引用被取代
    conn.execute("UPDATE core.fin_fact SET value = 123400000")
    conn.commit()
    report = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert (report.written, report.superseded) == (1, 1)
    assert [(r[4], r[8]) for r in _refs(conn)] == [("mismatch", False), ("match", True)]


@pytest.mark.db
def test_cli_dry_run_writes_nothing_and_yes_writes(
    conn: psycopg.Connection, temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_fact(conn, value="123400000")
    _add_table(conn)
    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    res = CliRunner().invoke(facts_group, ["table-check"])
    assert res.exit_code == 0, res.output
    assert "[dry run]" in res.output and "match: 1" in res.output
    logs = [json.loads(ln) for ln in res.output.splitlines() if ln.startswith("{")]
    assert logs and all({"run_id", "as_of", "entity_id", "event"} <= set(r) for r in logs)
    assert {r["entity_id"] for r in logs} == {"CN.688256", None}
    assert conn.execute("SELECT count(*) FROM core.fact_table_ref").fetchone() == (0,)
    res = CliRunner().invoke(facts_group, ["table-check", "--yes"])
    assert res.exit_code == 0, res.output
    assert "写入 1 行" in res.output
    assert conn.execute("SELECT count(*) FROM core.fact_table_ref").fetchone() == (1,)


_RAW = (
    "INSERT INTO core.fact_table_ref (fact_id, table_id, block_id, cell_row, cell_col, page,"
    " entity_id, metric_id, period, report_value, fact_value, tolerance, unit_scale,"
    " period_label, verdict, check_version, valid_from, known_at, owner_user, source,"
    " ingest_run_id) VALUES (%s, %s, %s, 1, 1, %s, 'CN.688256', 'revenue_total', '2024Q3',"
    " 100, 100, %s, %s, 'x', 'match', 'v', '2024-07-01', %s, %s, 'table_check', 'r')"
)


@pytest.mark.db
def test_tolerance_is_bounded_by_two_decimal_precision(conn: psycopg.Connection) -> None:
    fact_id = _add_fact(conn, value="123400000")
    table_id, block_id = _add_table(conn)

    def ins(tol: int, scale: int) -> None:
        conn.execute(_RAW, (fact_id, table_id, block_id, 1, tol, scale, FACT_KNOWN, None))

    # 整数亿元格：容差 5000 万 > max(6000, 0.005 × 1e8 = 50 万)
    with pytest.raises(psycopg.errors.CheckViolation, match="tolerance_bounded"):
        ins(50_000_000, 100_000_000)
    conn.rollback()
    # 6000 的下限在小倍数上仍可用；两位小数的亿元格（50 万）在界上
    ins(6000, 1)
    conn.execute("UPDATE core.fact_table_ref SET superseded_at = now()")
    ins(500_000, 100_000_000)
    with pytest.raises(psycopg.errors.CheckViolation, match="tolerance_bounded"):
        conn.execute(
            "UPDATE core.fact_table_ref SET tolerance = 500001 WHERE superseded_at IS NULL"
        )
    conn.rollback()


@pytest.mark.db
def test_match_row_requires_a_page(conn: psycopg.Connection) -> None:
    fact_id = _add_fact(conn, value="123400000")
    table_id, block_id = _add_table(conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="match_has_page"):
        conn.execute(_RAW, (fact_id, table_id, block_id, None, 6000, 10000, FACT_KNOWN, None))
    conn.rollback()


@pytest.mark.db
def test_private_owner_rows_cannot_be_written(conn: psycopg.Connection) -> None:
    fact_id = _add_fact(conn, value="123400000")
    table_id, block_id = _add_table(conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="public_only"):
        conn.execute(_RAW, (fact_id, table_id, block_id, 1, 6000, 10000, FACT_KNOWN, "u1"))
    conn.rollback()


@pytest.mark.db
def test_refs_of_a_superseded_fact_are_superseded_with_it(conn: psycopg.Connection) -> None:
    f1 = _add_fact(conn, value="123400000")
    _add_table(conn)
    run_fact_table_check(conn, ingest_run_id="t", write=True)
    # Wind 更正：旧事实 2024-11-30 被取代，新事实值对不上表格
    conn.execute("UPDATE core.fin_fact SET superseded_at = '2024-11-30' WHERE fact_id = %s", (f1,))
    conn.commit()
    f2 = _add_fact(conn, value="999999999")
    report = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert report.superseded == 1 and report.written == 1
    with as_of_session(conn, datetime(2025, 6, 1, tzinfo=UTC)) as s:
        rows = s.execute("SELECT fact_id, verdict FROM asof.fact_table_ref").fetchall()
    assert rows == [(f2, "mismatch")]
    # 取代时刻 = 事实被取代的时刻：更正之前旧引用仍可见
    with as_of_session(conn, datetime(2024, 11, 15, tzinfo=UTC)) as s:
        rows = s.execute("SELECT fact_id, verdict FROM asof.fact_table_ref").fetchall()
    assert (f1, "match") in rows
    again = run_fact_table_check(conn, ingest_run_id="t", write=True)
    assert (again.written, again.superseded) == (0, 0)
