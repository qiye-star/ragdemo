"""人工表格评测题导出：分层、配额、passage 不含 content_desc。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import psycopg
import pytest

from ragdemo.evals.human_labels import SHEET_COLUMNS
from ragdemo.evals.table_labels import (
    TABLE_QUOTA,
    _pages_by_ordinal,
    estimate_header_rows,
    export_table_label_sheet,
    is_usable_other,
    table_stratum,
)


def test_quota_matches_audit_proposal() -> None:
    assert dict(TABLE_QUOTA) == {
        "statement": 20,
        "holders_segments": 15,
        "cross_page_tail": 10,
        "multi_header": 5,
    }
    assert sum(TABLE_QUOTA.values()) == 50


def test_stratum_priority_cross_page_before_statement() -> None:
    md = "| 项目 | 本期 |\n|---|---|\n| 营业收入 | 1 |"
    assert table_stratum(md, n_pages=2, header_rows=1) == "cross_page_tail"
    assert table_stratum(md, n_pages=1, header_rows=1) == "statement"
    holders = "| 股东名称 | 持股数量 |\n|---|---|\n| 甲 | 1 |"
    assert table_stratum(holders, n_pages=1, header_rows=1) == "holders_segments"
    multi = "| 项目 | 2025年 |\n|---|---|\n| 甲 | 乙 |"
    assert table_stratum(multi, n_pages=1, header_rows=2) == "multi_header"
    assert table_stratum("| a | b |\n|---|---|\n| c | d |", n_pages=1, header_rows=1) == "other"


def test_estimate_header_rows_counts_leading_non_numeric_rows() -> None:
    html = (
        "<table><tr><td>项目</td><td colspan=2>2025年</td></tr>"
        "<tr><td></td><td>本期</td><td>上期</td></tr>"
        "<tr><td>营业收入</td><td>1,234.5</td><td>1,000</td></tr></table>"
    )
    assert estimate_header_rows(html) == 2
    assert estimate_header_rows("<table><tr><td>项目</td><td>金额</td></tr></table>") == 1
    assert estimate_header_rows(None) == 1


@pytest.mark.db
def test_export_has_columns_and_no_content_desc(corpus: psycopg.Connection, tmp_path: Path) -> None:
    secret = "ZZ独有描述串QQ"
    corpus.execute(
        "UPDATE core.doc_block SET content_desc = %s WHERE block_type = 'table'", (secret,)
    )
    corpus.commit()
    n_tables = corpus.execute(
        "SELECT count(*) FROM core.doc_block WHERE block_type = 'table'"
    ).fetchone()
    path = tmp_path / "t.csv"
    count = export_table_label_sheet(corpus, path, blob_root=tmp_path / "noblob")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == SHEET_COLUMNS
        rows = list(reader)
    assert len(rows) == count
    assert n_tables is not None and count <= int(n_tables[0])
    for r in rows:
        assert secret not in r["passage"]
        assert "页码：" in r["passage"]
        assert r["question"] == ""
    assert secret not in path.read_text(encoding="utf-8-sig")


def test_pages_by_ordinal_reads_split_pages_and_rejects_count_mismatch(tmp_path: Path) -> None:
    detail = [
        {"type": "paragraph"},
        {"type": "table", "page_id": 3, "split_section_page_ids": [4, 5]},
        {"type": "table", "page_id": 6},
    ]
    (tmp_path / "a.json").write_text(json.dumps({"result": {"detail": detail}}), encoding="utf-8")
    assert _pages_by_ordinal(tmp_path, "a.json", 2) == [(3, 5), (1, 6)]
    assert _pages_by_ordinal(tmp_path, "a.json", 3) == []
    assert _pages_by_ordinal(tmp_path, "missing.json", 2) == []
    assert _pages_by_ordinal(tmp_path, None, 2) == []


def test_is_usable_other_filters_tiny_and_non_numeric() -> None:
    ok = "| a | b |\n|---|---|\n| x | 1 |\n| y | 2 |"
    assert is_usable_other(ok)
    assert not is_usable_other("| a | b |\n|---|---|\n| x | 1 |")  # 只有 2 行
    assert not is_usable_other("| a |\n|---|\n| x |\n| y |")  # 单列
    assert not is_usable_other("| a | b |\n|---|---|\n| x | y |\n| z | w |")  # 无数字


def _add_tables(
    conn: psycopg.Connection, n: int, *, doc_type: str, owner_user: str | None, base: int
) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO core.doc_block (doc_id, block_type, section_path, ordinal, page, content,"
            " content_desc, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, source,"
            " ingest_run_id, owner_user) SELECT doc_id, 'table', '', %s, 1,"
            " %s, 'DESCMARK', true, entity_id, %s, publish_at, valid_from, known_at, source,"
            " 'r1', %s FROM core.document WHERE doc_id = 1",
            (
                base + i,
                f"| 项 | 值 |\n|---|---|\n| 甲{i}{doc_type} | {i + 1} |\n| 乙 | 2 |",
                doc_type,
                owner_user,
            ),
        )
    conn.commit()


@pytest.mark.db
def test_other_stratum_fills_to_80_and_excludes_research_private(
    corpus: psycopg.Connection, tmp_path: Path
) -> None:
    _add_tables(corpus, 100, doc_type="announcement", owner_user=None, base=1000)
    _add_tables(corpus, 5, doc_type="research_report", owner_user=None, base=2000)
    _add_tables(corpus, 5, doc_type="announcement", owner_user="u1", base=3000)
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    n = export_table_label_sheet(corpus, a, blob_root=tmp_path / "nb")
    export_table_label_sheet(corpus, b, blob_root=tmp_path / "nb")
    assert a.read_bytes() == b.read_bytes()  # 同种子确定
    text = a.read_text(encoding="utf-8-sig")
    assert n >= 80
    assert "[other]" in text
    assert "research_report" not in text
    assert "DESCMARK" not in text
    with a.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # 私有块的 doc_type 也是 announcement，靠 owner_user 排除：行数不超过公共块总数
    pub = corpus.execute(
        "SELECT count(*) FROM core.doc_block WHERE block_type='table' AND owner_user IS NULL"
        " AND doc_type <> 'research_report'"
    ).fetchone()
    assert pub is not None and len(rows) <= int(pub[0])
