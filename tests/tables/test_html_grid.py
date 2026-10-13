from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragdemo.parse.tiered.markup_normalize import normalize_markup
from ragdemo.tables.html_grid import grid_from_html
from ragdemo.tables.render import grid_markdown

HTML = (
    '<table><tr><th rowspan="2">项目</th><th colspan="2">2025年</th></tr>'
    "<tr><td>Q1</td><td>Q2</td></tr><tr><td>收入<br>合计</td><td>1.5</td><td>2.0</td></tr></table>"
)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "parse_engines" / "paddle_table_sample.json"


def test_html_to_grid_matches_routing_plan_markdown_semantics() -> None:
    g = grid_from_html(HTML, page=4, engine="L1:paddle:3.0")
    assert g is not None
    assert grid_markdown(g) == (
        "| 项目 | 2025年 | 2025年 |\n|---|---|---|\n| 项目 | Q1 | Q2 |\n| 收入 合计 | 1.5 | 2.0 |"
    )
    assert g.pages == (4,) and all(c.bbox is None for c in g.cells)
    assert (g.header_rows, g.header_cols) == (2, 1)


def test_nested_or_empty_tables_are_rejected() -> None:
    nested = "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>"
    assert grid_from_html(nested, page=1, engine="e") is None
    assert grid_from_html("<table></table>", page=1, engine="e") is None


def test_bad_span_attribute_falls_back_to_one() -> None:
    h = '<table><tr><td colspan="x">a</td><td>b</td></tr></table>'
    g = grid_from_html(h, page=1, engine="e")
    assert g is not None and g.n_cols == 2


def test_recorded_paddle_fixture_converts_faithfully() -> None:
    md = json.loads(FIXTURE.read_text(encoding="utf-8"))["parse"]["markdown"]
    norm = normalize_markup(md)
    assert grid_from_html(norm, page=2, engine="e") is None  # 表外的正文不得被吞掉
    table = norm[norm.index("<table") : norm.index("</table>") + len("</table>")]
    g = grid_from_html(table, page=2, engine="L1:paddle:3.7.0", provider_ref="r")
    assert g is not None and g.provider_ref == "r"
    assert [[c.text for c in g.cells if c.row == r] for r in range(g.n_rows)] == [
        ["ltem", "2024 2023"],
        ["Revenue 91.2", "60.1"],
        ["Net profit", "19.3 12.7"],
    ]
    assert g.holes() == [] and g.overlaps() == []


def test_empty_cells_kept_and_text_whitespace_collapsed() -> None:
    g = grid_from_html("<table><tr><td> a \n b </td><td></td></tr></table>", page=1, engine="e")
    assert g is not None and [c.text for c in g.cells] == ["a b", ""]


def test_entities_and_tbody_thead_supported() -> None:
    h = (
        "<table><thead><tr><th>A&amp;B</th></tr></thead>"
        "<tbody><tr><td>1&lt;2</td></tr></tbody></table>"
    )
    g = grid_from_html(h, page=1, engine="e")
    assert g is not None and [c.text for c in g.cells] == ["A&B", "1<2"]


@pytest.mark.parametrize(
    "bad",
    [
        # ragged rows (holes): padding would fabricate structure
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>",
        # rowspan runs past the last row
        '<table><tr><td rowspan="3">a</td><td>b</td></tr><tr><td>c</td></tr></table>',
        # row 1 has too many cells for the free slots (leaves a ragged extra column)
        '<table><tr><td rowspan="2">a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>',
        # stray text outside any cell
        "<table><tr>stray<td>a</td></tr></table>",
        "<table><td>a</td></table>",
        # colspan lands on a slot already taken by an earlier rowspan
        '<table><tr><td>x</td><td rowspan="2">y</td></tr><tr><td colspan="2">z</td></tr></table>',
        "<p>no table</p>",
        "",
        "<table><tr><td>a</td></tr></table><table><tr><td>b</td></tr></table>",
        "<table><tr><td>a",
        "<table><tr><td><img src=x></td></tr></table>",
    ],
)
def test_ambiguous_or_lossy_markup_is_refused(bad: str) -> None:
    assert grid_from_html(bad, page=1, engine="e") is None


def test_huge_spans_refused() -> None:
    h = '<table><tr><td colspan="100000">a</td></tr></table>'
    assert grid_from_html(h, page=1, engine="e") is None


def test_negative_span_is_one_zero_span_refused() -> None:
    assert (
        grid_from_html('<table><tr><td rowspan="0">a</td></tr></table>', page=1, engine="e") is None
    )
    h = '<table><tr><td rowspan="-1" colspan="-2">a</td></tr></table>'
    g = grid_from_html(h, page=1, engine="e")
    assert g is not None and (g.cells[0].row_span, g.cells[0].col_span) == (1, 1)


def test_invalid_page_returns_none() -> None:
    assert grid_from_html("<table><tr><td>a</td></tr></table>", page=0, engine="e") is None
