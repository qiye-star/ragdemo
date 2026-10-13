"""引擎 HTML 表格 → TableGrid（表格处理改进计划 T15，L1/L2 路径）。

保守原则：能忠实转换就转换，否则返回 None（调用方丢弃网格、保留块，绝不丢文档）。"看上去合理
但其实错了"的网格是假通过的形状，所以以下一律拒绝：嵌套表、多张表、表外有文字、行长度不一
（留洞）、跨度越界或冲突、rowspan/colspan 为 0、过大的跨度、单元格里出现会丢内容的标签
（img 等）、未闭合的标签、没有行或有空行。

单元格内只接受 <br>，其余标签/注释拒绝；tfoot 拒绝。

语义与 `parse.textin.table_markdown` 的展开规则一致：逐行从左找第一个未被占用的列放置，
跨度占位。跨度属性不是纯 ASCII 数字（含负数、空串）按 1；"0" 在 HTML 里表示"延伸到分组末尾"，
与我们的语义不同，拒绝。`<br>` 视为空格；文本 `" ".join(split())`；单元格 `bbox=None`、`page=page`。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

from ragdemo.tables.grid import GridCell, GridError, TableGrid
from ragdemo.tables.headers import infer_headers

MAX_SPAN: Final = 200
_SECTIONS: Final = frozenset({"thead", "tbody"})  # tfoot 拒绝：源顺序与渲染顺序不同


class _Refuse(Exception):
    pass


def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
    for key, value in attrs:
        if key.lower() == name:
            v = (value or "").strip()
            if not (v.isascii() and v.isdigit()):
                return 1
            n = int(v)
            if n == 0 or n > MAX_SPAN:
                raise _Refuse(f"{name}={v}")
            return n
    return 1


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables = 0
        self.depth = 0
        self.row: list[tuple[str, int, int]] | None = None
        self.cell: list[str] | None = None
        self.cell_span = (1, 1)
        self.rows: list[list[tuple[str, int, int]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables += 1
            self.depth += 1
            if self.depth > 1 or self.tables > 1:
                raise _Refuse("nested or multiple tables")
        elif self.depth == 0:
            raise _Refuse(f"<{tag}> outside table")
        elif tag in _SECTIONS:
            if self.row is not None or self.cell is not None:
                raise _Refuse("section inside row")
        elif tag == "tr":
            if self.row is not None or self.cell is not None:
                raise _Refuse("unclosed tr/td")
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None or self.cell is not None:
                raise _Refuse("td outside tr or unclosed td")
            self.cell = []
            self.cell_span = (_span(attrs, "rowspan"), _span(attrs, "colspan"))
        elif tag == "br":
            if self.cell is None:
                raise _Refuse("br outside cell")
            self.cell.append(" ")
        else:
            raise _Refuse(f"unsupported <{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.handle_starttag(tag, attrs)
        else:  # <td/> 等在 HTML 里不是自闭合：语义不确定
            raise _Refuse(f"self-closing <{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self.depth != 1 or self.row is not None or self.cell is not None:
                raise _Refuse("unbalanced </table>")
            self.depth = 0
        elif self.depth == 0:
            raise _Refuse(f"</{tag}> outside table")
        elif tag in _SECTIONS:
            if self.row is not None or self.cell is not None:
                raise _Refuse("unbalanced section")
        elif tag == "tr":
            if self.row is None or self.cell is not None or not self.row:
                raise _Refuse("unbalanced or empty tr")
            self.rows.append(self.row)
            self.row = None
        elif tag in ("td", "th"):
            if self.row is None or self.cell is None:
                raise _Refuse("unbalanced td")
            self.row.append((" ".join("".join(self.cell).split()), *self.cell_span))
            self.cell = None
        else:
            raise _Refuse(f"unsupported </{tag}>")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)
        elif data.strip():
            raise _Refuse("text outside cell")

    def handle_comment(self, data: str) -> None:
        if self.cell is not None:  # 单元格内的注释会把数字零宽粘连
            raise _Refuse("comment inside cell")

    def handle_decl(self, decl: str) -> None:
        raise _Refuse("declaration")

    def unknown_decl(self, data: str) -> None:
        raise _Refuse("declaration")

    def handle_pi(self, data: str) -> None:
        raise _Refuse("processing instruction")


def _place(rows: list[list[tuple[str, int, int]]], page: int | None) -> list[GridCell]:
    occupied: set[tuple[int, int]] = set()
    cells: list[GridCell] = []
    for r, row in enumerate(rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in occupied:
                c += 1
            if r + rs > len(rows):
                raise _Refuse("rowspan past last row")
            slots = {(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs)}
            if slots & occupied:
                raise _Refuse("span collision")
            occupied |= slots
            cells.append(GridCell(r, c, rs, cs, text, page=page, bbox=None))
            c += cs
    return cells


def grid_from_html(
    table_html: str, *, page: int | None, engine: str, provider_ref: str | None = None
) -> TableGrid | None:
    """HTML → 已推断表头的网格；任何无法忠实转换的情形返回 None。

    调用方须先跑 `parse.tiered.markup_normalize.normalize_markup`，并只传 `<table>…</table>`
    这一段（表外的正文会被拒绝，而不是吞掉）。单元格内只接受 `<br>`（视为空格）；任何其他
    标签或注释一律拒绝，因为零宽展平会把相邻数字粘成另一个数（`<p>2024</p><p>2023</p>`）。
    `tfoot` 拒绝：浏览器把它渲染在末尾，源顺序不同。
    """
    try:
        parser = _Collector()
        parser.feed(table_html)
        parser.close()
        if parser.tables != 1 or parser.depth != 0 or not parser.rows:
            return None
        cells = _place(parser.rows, page)
        grid = TableGrid(tuple(cells), source_engine=engine, provider_ref=provider_ref)
        if grid.holes() or grid.overlaps():
            return None
        return infer_headers(grid)
    except (_Refuse, GridError):
        return None
