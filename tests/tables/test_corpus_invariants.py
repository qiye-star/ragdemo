"""缓存的 TextIn 公告表格上的全量不变量（语料不在时跳过；不入库任何语料内容）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ragdemo.parse.textin import table_html_from_cells, table_markdown
from ragdemo.tables.grid import GridCell, TableGrid, grid_from_json, grid_to_json
from ragdemo.tables.headers import infer_headers, is_value_text, row_label
from ragdemo.tables.render import grid_html, grid_markdown

_ROOT = Path(__file__).resolve().parents[2] / "data" / "blob" / "parse" / "textin"
_YEAR = re.compile(r"^(?:19|20)[0-9]{2}$")


def _tables() -> list[list[dict[str, object]]]:
    out: list[list[dict[str, object]]] = []
    for f in sorted(_ROOT.rglob("*.json")):
        res = json.loads(f.read_text(encoding="utf-8"))
        res = res.get("result", res)
        for it in res.get("detail", []):
            if it.get("type") == "table" and it.get("cells"):
                out.append(it["cells"])
    return out


@pytest.mark.skipif(not _ROOT.exists(), reason="no cached TextIn corpus")
def test_corpus_grid_invariants() -> None:
    tables = _tables()
    assert tables
    for cells in tables:
        g = TableGrid(
            cells=tuple(
                GridCell(
                    int(c["row"]),  # type: ignore[call-overload]
                    int(c["col"]),  # type: ignore[call-overload]
                    int(c.get("row_span", 1)),  # type: ignore[call-overload]
                    int(c.get("col_span", 1)),  # type: ignore[call-overload]
                    str(c.get("text", "")),
                )
                for c in cells
            ),
            source_engine="textin:corpus",
        )
        assert grid_markdown(g) == table_markdown(cells)
        assert grid_html(g) == table_html_from_cells(cells)
        h = infer_headers(g)
        assert grid_from_json(json.loads(json.dumps(grid_to_json(h)))) == h
        for c in h.cells:
            if c.is_header and is_value_text(c.text):
                assert c.row < h.header_rows and _YEAR.match(c.text.strip()), c
        for r in range(h.n_rows):
            assert not any(is_value_text(p) for p in row_label(h, r).split(" / ") if p)
