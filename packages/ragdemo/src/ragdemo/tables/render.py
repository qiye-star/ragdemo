"""网格 → 检索用 Markdown / 结构化 HTML。委托 parse/textin.py 现有函数，保证与库内存量逐字相等。"""

from __future__ import annotations

from ragdemo.parse.textin import table_html_from_cells, table_markdown
from ragdemo.tables.grid import TableGrid


def grid_markdown(grid: TableGrid) -> str:
    return table_markdown(grid.as_cell_mappings())


def grid_html(grid: TableGrid) -> str:
    return table_html_from_cells(grid.as_cell_mappings())
