"""存量回填（T10）：只读缓存 TextIn JSON 与本地 PDF，零外部调用，幂等。"""

from __future__ import annotations

import json
from collections.abc import Collection
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from ragdemo.parse.textin import table_html_from_cells, table_markdown
from ragdemo.tables import backfill as bf
from ragdemo.tables.backfill import (
    BackfillMismatch,
    backfill_document,
    desc_provenance_of,
    match_tables,
    run_backfill,
)
from ragdemo.tables.verify_cells import PageWords
from ragdemo_core.blob import LocalBlobStore
from ragdemo_core.db.invariants import check_schema_invariants

CELLS = [{"row": 0, "col": 0, "row_span": 1, "col_span": 1, "text": "a", "page_id": 2}]
T1 = {"type": "table", "page_id": 2, "paragraph_id": 5, "content": 0, "cells": CELLS}
FOOTER_TABLE = {"type": "table", "page_id": 2, "paragraph_id": 6, "content": 1, "cells": CELLS}
EMPTY_TABLE = {"type": "table", "page_id": 3, "paragraph_id": 7, "content": 0, "cells": []}
PARA = {"type": "paragraph", "page_id": 2, "paragraph_id": 4, "content": 0, "text": "x"}


def test_match_applies_same_skip_rules_as_blocks_from_detail() -> None:
    got = match_tables([PARA, T1, FOOTER_TABLE, EMPTY_TABLE], [(101, 2, table_markdown(CELLS))])
    assert [(m.block_id, m.item_index) for m in got] == [(101, 1)]


def test_count_page_or_markdown_mismatch_aborts_document() -> None:
    with pytest.raises(BackfillMismatch, match="数量"):
        match_tables([T1], [])
    with pytest.raises(BackfillMismatch, match="页码"):
        match_tables([T1], [(101, 3, table_markdown(CELLS))])
    with pytest.raises(BackfillMismatch, match="Markdown"):
        match_tables([T1], [(101, 2, "| b |")])


def test_zero_based_page_ids_are_offset_like_blocks_from_detail() -> None:
    zero = {"type": "table", "page_id": 0, "paragraph_id": 1, "content": 0, "cells": CELLS}
    got = match_tables([zero], [(7, 1, table_markdown(CELLS))])
    assert [m.block_id for m in got] == [7]


def test_desc_provenance() -> None:
    assert desc_provenance_of("资产负债表表，列为 项目、期末余额，共 12 行。") == ("mock", "v1")
    assert desc_provenance_of("该表列出公司 2025 年前三季度主要会计数据。") == (
        "legacy:unrecorded",
        "legacy:unrecorded",
    )
    assert desc_provenance_of(None) is None
    assert desc_provenance_of("   ") is None


def test_mock_desc_pattern_matches_what_the_mock_describer_writes() -> None:
    from ragdemo.parse.describe import MockTableDescriber

    text = MockTableDescriber().describe(
        "| 项目 | 期末余额 |\n| a | 1 |", title="t", section_path="第三节 > 资产负债表"
    )
    assert bf.MOCK_DESC.match(text), text


# --- 页子集提取 ----------------------------------------------------------------------


def _two_page_pdf() -> bytes:
    from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.setFont("Helvetica", 10)
    c.drawString(50, 750, "Item")
    c.drawString(210, 750, "Amount")
    c.drawString(50, 710, "Revenue")
    c.drawString(210, 710, "1,234")
    c.showPage()
    c.setFont("Helvetica", 10)
    c.drawString(50, 750, "page two 99")
    c.showPage()
    c.save()
    return buf.getvalue()


def test_extract_page_words_subset_matches_full_extraction() -> None:
    from ragdemo.parse.local.layout import extract_pages
    from ragdemo.tables.verify_cells import extract_page_words, page_words_from_layouts

    pdf = _two_page_pdf()
    full = page_words_from_layouts(extract_pages(pdf))
    assert extract_page_words(pdf, None) == full
    assert extract_page_words(pdf, {2}) == {2: full[2]}
    assert extract_page_words(pdf, {1, 9}) == {1: full[1]}  # 越界页忽略
    assert extract_page_words(pdf, set()) == {}
    with pytest.raises(ValueError, match="PDF"):
        extract_page_words(b"nope", {1})


# --- 数据库：最小 TextIn JSON + 最小 PDF ----------------------------------------------


def _cell(row: int, col: int, text: str) -> dict[str, Any]:
    x0, x1 = 40 + 170 * col, 210 + 170 * col
    y0, y1 = 40 + 40 * row, 80 + 40 * row  # 像素 = pt：页宽高 600 x 800，第 0 行在 y≈50
    return {
        "row": row,
        "col": col,
        "row_span": 1,
        "col_span": 1,
        "text": text,
        "page_id": 1,
        "position": [x0, y0, x1, y0, x1, y1, x0, y1],
    }


# 页 1 的文字层：Item/Amount 在 y_pdf=750 → top≈40..50；Revenue/1,234 在 y_pdf=710 → top≈80..90
TABLE_CELLS = [
    _cell(0, 0, "Item"),
    _cell(0, 1, "Amount"),
    _cell(1, 0, "Revenue"),
    _cell(1, 1, "1,234"),
]
# 第二行的框在 y 80..120，文字中心约 85，落在框内；第一行框 40..80，中心约 45
PAYLOAD = {
    "metrics": [{"page_id": 1, "page_image_width": 600, "page_image_height": 800}],
    "result": {
        "total_page_number": 2,
        "markdown": "",
        "detail": [
            {
                "type": "paragraph",
                "page_id": 1,
                "paragraph_id": 0,
                "content": 0,
                "text": "单位：万元",
            },
            {"type": "table", "page_id": 1, "paragraph_id": 1, "content": 0, "cells": TABLE_CELLS},
        ],
    },
}


@pytest.fixture
def conn(corpus: psycopg.Connection) -> psycopg.Connection:
    return corpus


@pytest.fixture
def blob(tmp_path: Path) -> LocalBlobStore:
    b = LocalBlobStore(tmp_path)
    b.put("parse/d1.json", json.dumps(PAYLOAD).encode("utf-8"))
    b.put("raw/d1.pdf", _two_page_pdf())
    return b


@pytest.fixture
def wired(conn: psycopg.Connection) -> psycopg.Connection:
    """把 corpus 里文档 1 的表格块与文档指向上面的 JSON / PDF，内容与 JSON 一致。"""
    conn.execute(
        "UPDATE core.document SET parse_engine = 'textin:test', parse_json_ref = 'parse/d1.json',"
        " raw_ref = 'raw/d1.pdf' WHERE doc_id = 1"
    )
    conn.execute(
        "UPDATE core.doc_block SET content = %s, table_html = %s, content_desc = %s"
        " WHERE doc_id = 1 AND block_type = 'table'",
        (
            table_markdown(TABLE_CELLS),
            table_html_from_cells(TABLE_CELLS),
            "该表列出分部收入。",
        ),
    )
    conn.commit()
    return conn


def _live(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT block_id, verify_verdict, verify_version, desc_model, unit, unit_source, pages"
        " FROM core.doc_table WHERE superseded_at IS NULL ORDER BY block_id"
    ).fetchall()


def _count_all(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM core.doc_table").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_dry_run_computes_metrics_and_writes_nothing(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    rep = run_backfill(wired, blob, ingest_run_id="t", write=False)
    assert rep.failed == {}
    d = rep.docs[0]
    assert (d.tables, d.md_equal, d.html_equal, d.page_in_grid) == (1, 1, 1, 1)
    assert (d.inserted, d.skipped, d.superseded) == (1, 0, 0)  # 计划：会插入 1 张
    assert _count_all(wired) == 0


@pytest.mark.db
def test_write_is_idempotent_and_binds_context_and_checks_cells(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    first = run_backfill(wired, blob, ingest_run_id="t", write=True)
    d = first.docs[0]
    assert (d.inserted, d.skipped) == (1, 0)
    rows = _live(wired)
    assert len(rows) == 1
    _bid, verdict, version, desc_model, unit, unit_source, pages = rows[0]
    assert verdict == "text_layer_cells_ok"
    assert version == bf.VERIFY_CELLS_VERSION
    assert desc_model == "legacy:unrecorded"
    assert (unit, unit_source) == ("万元", "preceding_block")
    assert pages == [1]
    assert (d.numeric_cells, d.matched_cells, d.mismatched_tables) == (1, 1, 0)
    assert (d.unit_bound, d.numeric_tables) == (1, 1)
    assert check_schema_invariants(wired) == []

    second = run_backfill(wired, blob, ingest_run_id="t2", write=True)
    assert (second.docs[0].inserted, second.docs[0].skipped) == (0, 1)
    assert _count_all(wired) == 1


@pytest.mark.db
def test_verify_version_bump_supersedes_and_reinserts(
    wired: psycopg.Connection, blob: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_backfill(wired, blob, ingest_run_id="t", write=True)
    monkeypatch.setattr(bf, "VERIFY_CELLS_VERSION", "cellverify:v4")
    rep = run_backfill(wired, blob, ingest_run_id="t2", write=True)
    d = rep.docs[0]
    assert (d.superseded, d.inserted, d.deferred) == (1, 1, 0)
    old = wired.execute(
        "SELECT count(*) FROM core.doc_table WHERE superseded_at IS NOT NULL"
    ).fetchone()
    assert old == (1,)
    assert [r[2] for r in _live(wired)] == ["cellverify:v4"]
    assert check_schema_invariants(wired) == []


@pytest.mark.db
def test_row_whose_known_at_is_in_the_future_is_deferred_not_superseded(
    wired: psycopg.Connection, blob: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日精度的 known_at（公告日当天结束）在当天内 > now()：supersede 会撞 time_order CHECK。"""
    run_backfill(wired, blob, ingest_run_id="t", write=True)
    wired.execute("UPDATE core.document SET known_at = now() + interval '1 day' WHERE doc_id = 1")
    wired.execute("UPDATE core.doc_block SET known_at = now() + interval '1 day' WHERE doc_id = 1")
    wired.execute("UPDATE core.doc_table SET known_at = now() + interval '1 day' WHERE doc_id = 1")
    wired.commit()
    monkeypatch.setattr(bf, "VERIFY_CELLS_VERSION", "cellverify:v4")
    rep = run_backfill(wired, blob, ingest_run_id="t2", write=True)
    d = rep.docs[0]
    assert rep.failed == {}
    assert (d.deferred, d.superseded, d.inserted) == (1, 0, 0)
    assert [r[2] for r in _live(wired)] == ["cellverify:v3"]  # 旧行原样


@pytest.mark.db
def test_unchecked_row_is_upgraded_once_the_pdf_is_available(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    """主 dagster 入库路径不给 blob：网格 not_checked。--reverify 之外，默认运行也要补校验。"""
    wired.execute("UPDATE core.document SET raw_ref = NULL WHERE doc_id = 1")
    wired.commit()
    run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert [r[1] for r in _live(wired)] == ["not_checked"]
    again = run_backfill(wired, blob, ingest_run_id="t", write=True)  # 仍无 PDF：无事可做
    assert (again.docs[0].inserted, again.docs[0].skipped) == (0, 1)
    wired.execute("UPDATE core.document SET raw_ref = 'raw/d1.pdf' WHERE doc_id = 1")
    wired.commit()
    up = run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert (up.docs[0].superseded, up.docs[0].inserted) == (1, 1)
    assert [r[1] for r in _live(wired)] == ["text_layer_cells_ok"]


@pytest.mark.db
def test_reverify_rewrites_a_row_that_differs_at_current_versions(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    run_backfill(wired, blob, ingest_run_id="t", write=True)
    wired.execute("UPDATE core.doc_table SET unit = '元' WHERE superseded_at IS NULL")
    wired.commit()
    plain = run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert (plain.docs[0].inserted, plain.docs[0].skipped) == (0, 1)
    rev = run_backfill(wired, blob, ingest_run_id="t", write=True, reverify=True)
    assert (rev.docs[0].superseded, rev.docs[0].inserted) == (1, 1)
    assert [r[4] for r in _live(wired)] == ["万元"]
    clean = run_backfill(wired, blob, ingest_run_id="t", write=True, reverify=True)
    assert (clean.docs[0].inserted, clean.docs[0].skipped) == (0, 1)


@pytest.mark.db
def test_no_network_is_used(
    wired: psycopg.Connection, blob: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(httpx.Client, "send", boom)
    rep = run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert rep.failed == {}
    assert rep.totals()["inserted"] == 1


@pytest.mark.db
def test_only_the_pages_the_grid_needs_are_extracted(
    wired: psycopg.Connection, blob: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    real = bf.extract_page_words

    def spy(pdf: bytes, pages: Collection[int] | None) -> dict[int, PageWords]:
        seen.append(set(pages) if pages is not None else None)
        return real(pdf, pages)

    monkeypatch.setattr(bf, "extract_page_words", spy)
    run_backfill(wired, blob, ingest_run_id="t", write=False)
    assert seen == [{1}]


@pytest.mark.db
def test_missing_json_fails_only_that_document(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    wired.execute(
        "UPDATE core.document SET parse_engine = 'textin:test', parse_json_ref = 'nope.json'"
        " WHERE doc_id = 3"
    )
    wired.execute(
        "INSERT INTO core.doc_block (doc_id, block_type, ordinal, page, content, is_leaf,"
        " entity_id, doc_type, publish_at, valid_from, known_at, source, ingest_run_id)"
        " SELECT d.doc_id, 'table', 9, 1, '| x |', true, d.entity_id, d.doc_type, d.publish_at,"
        " d.valid_from, d.known_at, 'mock', 'r1' FROM core.document d WHERE d.doc_id = 3"
    )
    wired.commit()
    rep = run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert set(rep.failed) == {3}
    assert [d.doc_id for d in rep.docs] == [1]
    assert _count_all(wired) == 1


@pytest.mark.db
def test_mismatching_markdown_aborts_that_document_and_writes_nothing(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    wired.execute(
        "UPDATE core.doc_block SET content = '| 改过 |' WHERE doc_id = 1 AND block_type = 'table'"
    )
    wired.commit()
    rep = run_backfill(wired, blob, ingest_run_id="t", write=True)
    assert set(rep.failed) == {1}
    assert "Markdown" in rep.failed[1]
    assert _count_all(wired) == 0


@pytest.mark.db
def test_superseded_documents_and_non_textin_documents_are_not_selected(
    wired: psycopg.Connection, blob: LocalBlobStore
) -> None:
    wired.execute("UPDATE core.document SET parse_engine = 'vendor:cninfo' WHERE doc_id = 1")
    wired.commit()
    assert run_backfill(wired, blob, ingest_run_id="t", write=True).docs == []
    wired.execute("UPDATE core.document SET parse_engine = 'textin:test' WHERE doc_id = 1")
    wired.execute("UPDATE core.doc_block SET superseded_at = now() WHERE doc_id = 1")
    wired.execute("UPDATE core.document SET superseded_at = now() WHERE doc_id = 1")
    wired.commit()
    assert run_backfill(wired, blob, ingest_run_id="t", write=True).docs == []


@pytest.mark.db
def test_doc_ids_filter(wired: psycopg.Connection, blob: LocalBlobStore) -> None:
    rep = run_backfill(wired, blob, ingest_run_id="t", write=False, doc_ids=[2])
    assert rep.docs == [] and rep.failed == {}
    one = backfill_document(wired, blob, 1, ingest_run_id="t", write=False)
    assert one.tables == 1


@pytest.mark.db
def test_cli_dry_run_then_yes(
    wired: psycopg.Connection,
    blob: LocalBlobStore,
    temp_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from click.testing import CliRunner

    from ragdemo.cli import main
    from ragdemo.ingest import cli as docs_cli

    monkeypatch.setenv("RAGDEMO_DSN", temp_db)
    monkeypatch.setattr(docs_cli, "_load_config", lambda: SimpleNamespace(blob_root=tmp_path))
    out = tmp_path / "r.json"
    r = CliRunner().invoke(main, ["docs", "backfill-tables", "--json-out", str(out)])
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output and _count_all(wired) == 0
    r = CliRunner().invoke(main, ["docs", "backfill-tables", "--yes", "--json-out", str(out)])
    assert r.exit_code == 0, r.output
    assert _count_all(wired) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["totals"]["inserted"] == 1
