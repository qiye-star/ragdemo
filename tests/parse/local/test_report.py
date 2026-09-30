from ragdemo.parse.local.pipeline import PageResult
from ragdemo.parse.local.report import (
    DOCS_HEADER,
    PAGES_HEADER,
    RESEARCH_HEADER,
    docs_err_row,
    docs_row,
    page_rows,
    research_err_row,
    research_row,
    sanitize,
)


def _p(page: int, status: str, reason: str = "", v: list[str] | None = None) -> PageResult:
    return PageResult(page, status, "md", "src", v or [], reason)  # type: ignore[arg-type]


OK = [_p(1, "ok"), _p(2, "ok")]
MIXED = [_p(1, "ok"), _p(2, "closure_failed", "数字闭合失败：['1']", ["1"])]
M = (0.9, 0.95, 0.99, 1.0)


def _cols(line: str) -> int:
    return line.count("\t") + 1


def test_docs_rows_have_the_header_column_count_in_every_case() -> None:
    n = _cols(DOCS_HEADER)
    assert _cols(docs_row("d", "t", OK, M)) == n
    assert _cols(docs_row("d", "t", MIXED, M)) == n
    assert _cols(docs_row("d", "t", [], None)) == n
    assert _cols(docs_err_row("d", "t", RuntimeError("x"))) == n


def test_metrics_are_blank_unless_the_document_is_fully_accepted() -> None:
    assert docs_row("d", "t", OK, M).split("\t")[7:] == [
        "1",
        "0.9000",
        "0.9500",
        "0.9900",
        "1.0000",
    ]
    assert docs_row("d", "t", MIXED, M).split("\t")[7:] == ["0", "", "", "", ""]


def test_title_with_tabs_and_newlines_cannot_corrupt_the_row() -> None:
    row = docs_row("d", "a\tb\nc", OK, M)
    assert _cols(row) == _cols(DOCS_HEADER) and "\n" not in row


def test_research_rows_have_the_header_column_count() -> None:
    n = _cols(RESEARCH_HEADER)
    assert _cols(research_row("f\t.pdf", MIXED)) == n
    assert _cols(research_err_row("f.pdf", ValueError())) == n


def test_page_rows_only_list_non_ok_pages_sanitized_and_truncated() -> None:
    pages = [_p(1, "ok"), _p(2, "structure_failed", "x\ty\n" + "z" * 400)]
    (row,) = page_rows("doc\t1", pages)
    cells = row.split("\t")
    assert len(cells) == _cols(PAGES_HEADER) == 5
    assert cells[:3] == ["doc 1", "2", "structure_failed"] and len(cells[3]) == 300
    assert page_rows("d", OK) == []


def test_sanitize_truncates() -> None:
    assert sanitize("a\tb") == "a b" and len(sanitize("x" * 999)) == 300
