from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ragdemo.parse.local.compare import HeadingMatch
from ragdemo.parse.local.det_report import (
    DET_DOCS_HEADER,
    DET_RESEARCH_HEADER,
    DetMetrics,
    ExtraNumbers,
    PageInfo,
    band_numbers,
    classify_extra_numbers,
    det_docs_err_row,
    det_docs_row,
    det_research_err_row,
    det_research_row,
    parse_run_args,
)
from ragdemo.parse.local.layout import PageLayout, Word
from ragdemo.parse.local.pipeline import PageResult


def _cols(line: str) -> int:
    return line.count("\t") + 1


def _p(page: int, status: str, reason: str = "") -> PageResult:
    return PageResult(page, status, "md", "src", [], reason)  # type: ignore[arg-type]


OK = [_p(1, "ok"), _p(2, "ok")]
MIXED = [_p(1, "ok"), _p(2, "layout_unsupported", "多栏版面"), _p(3, "no_text_layer", "x")]
M = DetMetrics(0.95, 0.99, 1.0, 0.9, HeadingMatch(4, 5, 6, 3), ExtraNumbers(1, 2, 3))


def test_det_rows_have_the_header_column_count_in_every_case() -> None:
    n = _cols(DET_DOCS_HEADER)
    assert _cols(det_docs_row("d", "t\tx", OK, M, 1.5)) == n
    assert _cols(det_docs_row("d", "t", MIXED, M, 1.5)) == n
    assert _cols(det_docs_row("d", "t", [], None, 0.0)) == n
    assert _cols(det_docs_err_row("d", "t", RuntimeError("x"))) == n
    m = _cols(DET_RESEARCH_HEADER)
    assert _cols(det_research_row("f\t.pdf", MIXED, 2.0)) == m
    assert _cols(det_research_err_row("f.pdf", ValueError())) == m


def test_det_docs_row_counts_all_four_statuses_including_layout_unsupported() -> None:
    cells = dict(
        zip(
            DET_DOCS_HEADER.split("\t"),
            det_docs_row("d", "t", MIXED, M, 1.5).split("\t"),
            strict=True,
        )
    )
    assert (cells["ok"], cells["no_text_layer"], cells["layout_unsupported"]) == ("1", "1", "1")
    assert cells["closure_failed"] == "0" and cells["accepted"] == "0"
    assert cells["coverage"] == "" and cells["h_ref"] == "" and cells["seconds"] == "1.50"


def test_det_docs_row_has_metrics_only_for_accepted_documents() -> None:
    cells = dict(
        zip(
            DET_DOCS_HEADER.split("\t"), det_docs_row("d", "t", OK, M, 1.5).split("\t"), strict=True
        )
    )
    assert cells["accepted"] == "1" and cells["coverage"] == "0.9500"
    assert (cells["h_ref"], cells["h_cand"], cells["h_matched"], cells["h_level_agree"]) == (
        "6",
        "5",
        "4",
        "3",
    )
    assert (cells["extra_hf"], cells["extra_table"], cells["extra_other"]) == ("1", "2", "3")


def test_band_numbers_are_the_numbers_in_the_top_and_bottom_bands_of_the_page() -> None:
    words = [
        Word("688041", 10, 50, 5, 15),  # 顶带
        Word("body", 10, 50, 400, 410),
        Word("123", 10, 50, 400, 410),
        Word("7", 10, 50, 785, 795),  # 底带
    ]
    layout = PageLayout(1, 600, 800, words, "")
    assert band_numbers(layout) == Counter({"688041": 1, "7": 1})


def test_extra_numbers_are_classified_table_then_page_edge_then_other() -> None:
    reference = "营业收入 91.2 利润 19.3"
    pages = [
        PageInfo(
            "正文 91.2 与 19.3\n| 项目 | 55.5 |\n|---|---|\n| 行 | 66.6 |\n688041 边缘 77.7",
            Counter({"688041": 1}),
        ),
        PageInfo("另一页 88.8 以及 66.6", Counter()),
    ]
    extra = classify_extra_numbers(reference, pages)
    # 多出：55.5、66.6（表格）；688041（页边带）；77.7、第二页的 88.8 与 66.6（其他）
    assert extra == ExtraNumbers(hf=1, table=2, other=3)


def test_classify_extra_numbers_is_zero_when_the_candidate_is_a_subset() -> None:
    assert classify_extra_numbers(
        "91.2 19.3 5", [PageInfo("91.2 19.3", Counter())]
    ) == ExtraNumbers(0, 0, 0)


def test_run_args_default_engine_is_llm_and_deterministic_is_selectable() -> None:
    a = parse_run_args(["docs"])
    assert (a.mode, a.engine, a.directory) == ("docs", "llm", None)
    b = parse_run_args(["research", "some/dir", "--engine", "deterministic"])
    assert (b.mode, b.engine, b.directory) == ("research", "deterministic", Path("some/dir"))
    c = parse_run_args(["--engine=deterministic", "docs"])
    assert (c.mode, c.engine) == ("docs", "deterministic")
    assert parse_run_args([]).mode == "docs"


@pytest.mark.parametrize(
    "argv",
    [["docs", "--engine", "gpt"], ["research"], ["bogus"], ["docs", "--engine"], ["docs", "--wat"]],
)
def test_run_args_reject_bad_input_with_a_usage_message(argv: list[str]) -> None:
    with pytest.raises(SystemExit, match="用法"):
        parse_run_args(argv)


def test_det_docs_row_records_the_doc_type_in_the_last_column() -> None:
    row = det_docs_row("d", "t", OK, M, 1.5, doc_type="announcement")
    cells = dict(zip(DET_DOCS_HEADER.split("\t"), row.split("\t"), strict=True))
    assert cells["doc_type"] == "announcement" and cells["seconds"] == "1.50"
    assert _cols(row) == _cols(DET_DOCS_HEADER)
