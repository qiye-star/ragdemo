"""路由旁路 JSON → 按页引擎与层级（表格处理改进计划 T15）。

旁路 JSON 由分级路由写在 `core.document.parse_json_ref`，键 `"routes"`。读不到、不是 JSON、
没有该键、条目非法 → 该页没有路由（调用方回落到文档级引擎、层级未知）。同一页出现多条路由
是自相矛盾的记录：整页丢弃，不猜。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ragdemo.parse.tiered.types import PageRoute, Tier, route_from_dict
from ragdemo.parse.tiered.verify import Verdict
from ragdemo_core.blob import BlobNotFound, BlobStore


def routes_from_sidecar(blob: BlobStore, json_ref: str) -> dict[int, PageRoute]:
    try:
        data: Any = json.loads(blob.get(json_ref).decode("utf-8"))
    except (
        BlobNotFound,
        OSError,
        ValueError,
    ):  # UnicodeDecodeError/JSONDecodeError 都是 ValueError
        return {}
    raw = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return {}
    routes: list[PageRoute] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            routes.append(route_from_dict(item))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    dup = {p for p, n in Counter(r.page for r in routes).items() if n > 1}
    return {r.page: r for r in routes if r.page not in dup}


def table_engine(route: PageRoute | None, fallback: str) -> str:
    """只返回引擎标签；层级不在这里给（见 `tier_for_summary`）。"""
    return fallback if route is None else f"{route.tier}:{route.engine}"


def tier_for_summary(route: PageRoute | None, verdict: Verdict | None) -> Tier | None:
    """传给 `summarize_grid(..., tier=)` 的层级。旁路 JSON 的字符串本身什么都不授予。

    L2：必须持有 `verify_dual` 的 Verdict 且 ok；L1：必须持有页级文字层校验
    （`verify_against_text_layer`）的 Verdict 且 ok。无路由、无 verdict、verdict 不 ok、
    或层级是 L0/L3（L3 的网格来自 TextIn cells，不走本路径）→ None。
    """
    if route is None or verdict is None or not verdict.ok:
        return None
    return route.tier if route.tier in ("L1", "L2") else None
