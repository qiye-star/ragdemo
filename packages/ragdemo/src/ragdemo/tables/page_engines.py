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


def table_engine(route: PageRoute | None, fallback: str) -> tuple[str, Tier | None]:
    if route is None:
        return fallback, None
    return f"{route.tier}:{route.engine}", route.tier
