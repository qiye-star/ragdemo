"""TextIn detail[] 表格条目 → TableGrid（每格保留自己的 page_id 与 8 点 position）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ragdemo.tables.grid import BBox, GridCell, TableGrid
from ragdemo.tables.headers import infer_headers


def normalized_bbox(position: Sequence[float] | None, dims: tuple[int, int] | None) -> BBox | None:
    """与 parse.textin._bbox 同一公式：像素四角八点 → 归一化轴对齐框。"""
    if not position or len(position) != 8 or dims is None:
        return None
    width, height = dims
    if not width or not height:
        return None
    xs, ys = position[0::2], position[1::2]
    box = (min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height)
    # 页外/负坐标：宁可没有 bbox，也不夹紧（夹紧后的框看似合理却是错的 = 假通过）。
    if not all(0.0 <= v <= 1.0 for v in box):
        return None
    return box


def textin_locator(json_ref: str | None, item: Mapping[str, Any]) -> str | None:
    if json_ref is None:
        return None
    return f"{json_ref}#page_id={item.get('page_id')}&paragraph_id={item.get('paragraph_id')}"


def grid_from_textin_item(
    item: Mapping[str, Any],
    *,
    page_offset: int,
    page_dims: Mapping[int, tuple[int, int]],
    engine: str,
    provider_ref: str | None,
) -> TableGrid | None:
    raw = item.get("cells") or []
    if not raw:
        return None
    multi_page = len(item.get("split_section_page_ids") or []) > 1
    cells: list[GridCell] = []
    for c in raw:
        pid = c.get("page_id")
        if pid is None and not multi_page:
            pid = item.get("page_id")
        # 跨页表里缺 page_id 的单元格：页码未知，不猜（page=None、bbox=None）。
        page = int(pid) + page_offset if pid is not None else None
        cells.append(
            GridCell(
                row=int(c["row"]),
                col=int(c["col"]),
                row_span=int(c.get("row_span", 1)),
                col_span=int(c.get("col_span", 1)),
                text=str(c.get("text", "")),
                page=page,
                bbox=normalized_bbox(c.get("position"), page_dims.get(page) if page else None),
            )
        )
    return infer_headers(
        TableGrid(cells=tuple(cells), source_engine=engine, provider_ref=provider_ref)
    )
