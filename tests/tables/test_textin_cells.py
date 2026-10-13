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


def _item(cell: dict[str, object], **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "row": 0,
        "col": 0,
        "row_span": 1,
        "col_span": 1,
        "text": "SECRETCELL",
        "page_id": 1,
        "position": [10, 10, 50, 10, 50, 20, 10, 20],
    }
    base.update(cell)
    ok = {
        "row": 1,
        "col": 0,
        "row_span": 1,
        "col_span": 1,
        "text": "ok",
        "page_id": 1,
        "position": [10, 30, 50, 30, 50, 40, 10, 40],
    }
    return {"type": "table", "page_id": 1, "paragraph_id": 3, "cells": [base, ok], **extra}


def test_bbox_outside_page_is_none_never_clamped() -> None:
    assert normalized_bbox([0, 0, 120, 0, 120, 10, 0, 10], (100, 100)) is None
    assert normalized_bbox([-5, 0, 10, 0, 10, 10, -5, 10], (100, 100)) is None
    assert normalized_bbox([0, 0, 100, 0, 100, 100, 0, 100], (100, 100)) == (0.0, 0.0, 1.0, 1.0)


import logging  # noqa: E402

import pytest  # noqa: E402

from ragdemo.parse.textin import blocks_from_detail, table_html_from_cells  # noqa: E402

_PROBES = {
    "beyond_page": {"position": [10, 10, 120, 10, 120, 20, 10, 20]},
    "negative_position": {"position": [-5, 10, 50, 10, 50, 20, -5, 20]},
    "cell_page_zero": {"page_id": 0},
    "row_span_zero": {"row_span": 0},
    "col_span_zero": {"col_span": 0},
    "negative_row": {"row": -1},
}


@pytest.mark.parametrize("name", sorted(_PROBES))
def test_bad_cells_never_break_block_output(name: str, caplog: pytest.LogCaptureFixture) -> None:
    item = _item(_PROBES[name])
    with caplog.at_level(logging.WARNING):
        (block,) = blocks_from_detail([item], page_dims={1: (100, 100)})
    assert block.content == table_markdown(item["cells"])  # type: ignore[arg-type]
    assert block.table_html == table_html_from_cells(item["cells"])  # type: ignore[arg-type]
    if name in ("beyond_page", "negative_position"):
        assert block.table is not None
        assert block.table.grid.cells[0].bbox is None
        assert block.table.grid.cells[1].bbox is not None
    elif name == "cell_page_zero":
        # 页码 0 在 1 基文档里不可能；整页偏移=0 时 page=0 非法 -> 无网格
        assert block.table is None
    else:
        assert block.table is None
    if block.table is None:
        rec = [r for r in caplog.records if "table grid" in r.getMessage()]
        assert rec and "SECRETCELL" not in rec[0].getMessage()


def test_cell_without_page_id_in_multipage_table_is_unlocated() -> None:
    item = dict(ITEM, cells=[dict(c) for c in ITEM["cells"]])  # type: ignore[call-overload]
    del item["cells"][3]["page_id"]  # type: ignore[attr-defined]
    g = grid_from_textin_item(item, page_offset=0, page_dims=DIMS, engine="e", provider_ref=None)
    assert g is not None
    tail = g.cell_at(1, 1)
    assert tail is not None and tail.page is None and tail.bbox is None
    head = g.cell_at(0, 0)
    assert head is not None and head.page == 1 and head.bbox is not None


def test_cell_without_page_id_in_single_page_table_uses_item_page() -> None:
    item = _item({})
    del item["cells"][0]["page_id"]  # type: ignore[attr-defined]
    g = grid_from_textin_item(
        item, page_offset=0, page_dims={1: (100, 100)}, engine="e", provider_ref=None
    )
    assert g is not None and g.cells[0].page == 1
