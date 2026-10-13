# ruff: noqa: E501
"""TextIn 页码基（0 基 / 1 基）核对（L0 + TextIn 分页接入计划 T4 风险，T9 落实）。

`textin._page_offset` 只看 detail[] 的最小 page_id 来猜页码基：若响应是 0 基、而子文档首页恰好没有
任何条目，最小 page_id 会是 1，被猜成 1 基，整段块的页码整体错一页（引用页码错误，CLAUDE.md §0 不允许）。
顶层 metrics[] 给每一页都列了 page_id（含空页），用它的最小值作为页码基的独立证据；
没有 metrics 时退而检查 page_id 不得超出子文档页数。纯函数，不改任何数据。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _ids(rows: Sequence[Mapping[str, Any]] | None) -> list[int]:
    return [int(r["page_id"]) for r in rows or [] if r.get("page_id") is not None]


def page_offset_problem(payload: Mapping[str, Any], n_pages: int) -> str | None:
    """返回问题描述；一致则 None。n_pages = 这次送出的页数（整份时即总页数）。"""
    detail = (payload.get("result") or {}).get("detail") or []
    ids = _ids(detail)
    guessed = 1 if ids and min(ids) == 0 else 0  # 与 textin._page_offset 同一口径
    metric_ids = _ids(payload.get("metrics"))
    if metric_ids:
        base = min(metric_ids)
        if base not in (0, 1):
            return f"metrics 最小 page_id={base}，既不是 0 基也不是 1 基"
        truth = 1 if base == 0 else 0
        if ids and guessed != truth:
            return f"页码基不一致：detail 推断偏移 {guessed}，metrics 显示偏移 {truth}"
        return None
    if ids and max(ids) + guessed > n_pages:
        return f"无 metrics 可核对，且 page_id 最大值 {max(ids)} 超出送出的 {n_pages} 页"
    return None
