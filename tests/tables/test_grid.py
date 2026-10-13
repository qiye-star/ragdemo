from __future__ import annotations

from typing import Any

import pytest

from ragdemo.parse.textin import table_html_from_cells, table_markdown
from ragdemo.tables.grid import (
    GridCell,
    GridError,
    TableGrid,
    grid_from_json,
    grid_to_json,
    remap_grid_pages,
)
from ragdemo.tables.render import grid_html, grid_markdown

TEXTIN_CELLS: list[dict[str, Any]] = [
    {"row": 0, "col": 0, "row_span": 2, "col_span": 1, "text": "项目", "page_id": 3},
    {"row": 0, "col": 1, "row_span": 1, "col_span": 2, "text": "2025年1-9月", "page_id": 3},
    {"row": 1, "col": 1, "row_span": 1, "col_span": 1, "text": "金额", "page_id": 3},
    {"row": 1, "col": 2, "row_span": 1, "col_span": 1, "text": "占比", "page_id": 3},
    {"row": 2, "col": 0, "row_span": 1, "col_span": 1, "text": "营业收入|含税", "page_id": 4},
    {"row": 2, "col": 1, "row_span": 1, "col_span": 1, "text": "1,234.50\n", "page_id": 4},
    {"row": 2, "col": 2, "row_span": 1, "col_span": 1, "text": "", "page_id": 4},
]


def _grid() -> TableGrid:
    return TableGrid(
        cells=tuple(
            GridCell(c["row"], c["col"], c["row_span"], c["col_span"], c["text"], page=c["page_id"])
            for c in TEXTIN_CELLS
        ),
        source_engine="textin:test",
    )


def test_rendering_is_byte_identical_to_existing_textin_functions() -> None:
    g = _grid()
    assert grid_markdown(g) == table_markdown(TEXTIN_CELLS)
    assert grid_html(g) == table_html_from_cells(TEXTIN_CELLS)


def test_shape_pages_holes() -> None:
    g = _grid()
    assert (g.n_rows, g.n_cols) == (3, 3)
    assert g.pages == (3, 4)
    assert g.holes() == []
    assert g.overlaps() == []
    cell = g.cell_at(1, 0)
    assert cell is not None and cell.text == "项目"


def test_hole_detected() -> None:
    g = TableGrid(cells=(GridCell(0, 0, 1, 1, "a"), GridCell(1, 1, 1, 1, "b")), source_engine="x")
    assert g.holes() == [(0, 1), (1, 0)]


def test_invalid_cells_rejected() -> None:
    with pytest.raises(GridError):
        GridCell(-1, 0, 1, 1, "a")
    with pytest.raises(GridError):
        GridCell(0, 0, 0, 1, "a")
    with pytest.raises(GridError):
        TableGrid(cells=(), source_engine="x")


def test_json_round_trip_preserves_everything() -> None:
    g = TableGrid(
        cells=(
            GridCell(
                0,
                0,
                1,
                2,
                "a",
                page=2,
                bbox=(0.1, 0.2, 0.3, 0.4),
                is_header=True,
                check="no_numbers",
            ),
        ),
        source_engine="textin:v",
        provider_ref="parse/textin/h/fp.json#page_id=2&paragraph_id=7",
        header_rows=1,
        header_cols=0,
    )
    assert grid_from_json(grid_to_json(g)) == g


def test_remap_pages_for_sub_pdf() -> None:
    g = _grid()
    assert remap_grid_pages(g, {3: 10, 4: 11}).pages == (10, 11)
    with pytest.raises(GridError):
        remap_grid_pages(g, {3: 10})


def test_cell_validation() -> None:
    for bad in (
        dict(bbox=(180.0, 1162.0, 1058.0, 1525.0)),  # pixel box
        dict(bbox=(0.5, 0.5, 0.1, 0.1)),  # inverted
        dict(bbox=(0.1, 0.1, 0.2, float("nan"))),
        dict(page=0),
        dict(page=-1),
        dict(check="matched"),
    ):  # fmt: skip
        with pytest.raises(GridError):
            GridCell(0, 0, 1, 1, "a", **bad)
    with pytest.raises(GridError):
        GridCell(0, 0, 1, 1, None)  # type: ignore[arg-type]
    with pytest.raises(GridError):
        GridCell(0.5, 0, 1, 1, "a")  # type: ignore[arg-type]


def test_grid_header_bounds() -> None:
    cell = GridCell(0, 0, 1, 1, "a")
    for rows, cols in ((2, 0), (-1, 0), (0, 2), (0, -1)):
        with pytest.raises(GridError):
            TableGrid(cells=(cell,), source_engine="x", header_rows=rows, header_cols=cols)


def test_from_json_is_strict() -> None:
    base = {"r": 0, "c": 0, "rs": 1, "cs": 1, "t": "a"}

    def mk(**over: object) -> dict[str, Any]:
        return {"v": 1, "engine": "x", "cells": [{**base, **over}]}

    assert grid_from_json(mk()).cells[0].text == "a"
    for over in (
        {"b": [0.1, 0.2, 0.3]}, {"h": "false"}, {"k": "MATCH"}, {"r": True}, {"r": "0"},
        {"t": 5}, {"p": "2"}, {"p": 0}, {"b": [180, 1162, 1058, 1525]},
    ):  # fmt: skip
        with pytest.raises(GridError):
            grid_from_json(mk(**over))
    with pytest.raises(GridError):
        grid_from_json({"v": "1", "engine": "x", "cells": [base]})
    with pytest.raises(GridError):
        grid_from_json({"v": 1, "cells": [base]})
    with pytest.raises(GridError):
        grid_from_json({"v": 1, "engine": "x", "cells": [base], "hr": 5})


def test_grid_version_tied_to_json_v() -> None:
    from ragdemo.tables.grid import GRID_VERSION

    assert f"tablegrid:v{grid_to_json(_grid())['v']}" == GRID_VERSION
