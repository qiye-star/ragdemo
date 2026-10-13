from __future__ import annotations

from ragdemo.parse.textin import _bbox, table_markdown
from ragdemo.tables.render import grid_markdown
from ragdemo.tables.textin_cells import grid_from_textin_item, normalized_bbox, textin_locator

ITEM = {
    "type": "table",
    "page_id": 1,
    "paragraph_id": 15,
    "position": [180, 1162, 1058, 1162, 1060, 1525, 181, 1526],
    "split_section_page_ids": [1, 2],
    "cells": [
        {
            "row": 0,
            "col": 0,
            "row_span": 1,
            "col_span": 1,
            "text": "项目",
            "type": "cell",
            "page_id": 1,
            "position": [180, 1162, 600, 1162, 600, 1200, 180, 1200],
        },
        {
            "row": 0,
            "col": 1,
            "row_span": 1,
            "col_span": 1,
            "text": "本报告期",
            "type": "cell",
            "page_id": 1,
            "position": [600, 1162, 1058, 1162, 1058, 1200, 600, 1200],
        },
        {
            "row": 1,
            "col": 0,
            "row_span": 1,
            "col_span": 1,
            "text": "营业收入",
            "type": "cell",
            "page_id": 2,
            "position": [180, 153, 600, 153, 600, 190, 180, 190],
        },
        {
            "row": 1,
            "col": 1,
            "row_span": 1,
            "col_span": 1,
            "text": "1,234.50",
            "type": "cell",
            "page_id": 2,
            "position": [600, 153, 1058, 153, 1058, 190, 600, 190],
        },
    ],
}
DIMS = {1: (1190, 1683), 2: (1190, 1683)}


def test_each_cell_keeps_its_own_page_and_bbox() -> None:
    g = grid_from_textin_item(
        ITEM, page_offset=0, page_dims=DIMS, engine="textin:t+fp", provider_ref="r#x"
    )
    assert g is not None
    assert g.pages == (1, 2)
    tail = g.cell_at(1, 1)
    assert tail is not None and tail.page == 2
    assert tail.bbox == _bbox([600, 153, 1058, 153, 1058, 190, 600, 190], (1190, 1683))
    assert (g.header_rows, g.header_cols) == (1, 1)
    assert grid_markdown(g) == table_markdown(ITEM["cells"])


def test_bbox_formula_matches_textin() -> None:
    pos = [10.0, 20.0, 110.0, 20.0, 110.0, 70.0, 10.0, 70.0]
    assert normalized_bbox(pos, (200, 100)) == _bbox(pos, (200, 100))
    assert normalized_bbox(None, (200, 100)) is None


def test_locator_uses_page_and_paragraph_id() -> None:
    assert (
        textin_locator("parse/textin/h/fp.json", ITEM)
        == "parse/textin/h/fp.json#page_id=1&paragraph_id=15"
    )
    assert textin_locator(None, ITEM) is None


def test_no_cells_no_grid() -> None:
    assert (
        grid_from_textin_item(
            {"type": "table", "cells": []},
            page_offset=0,
            page_dims={},
            engine="t",
            provider_ref=None,
        )
        is None
    )
