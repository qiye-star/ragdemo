"""iFind 公告降级层（公告双源接入计划 Task 6，ADR-0012/0013 分工：A 股公告主源
是巨潮资讯网，iFind `search_notice` 排在 Wind 之后，巨潮和 Wind 都不可用时才顶上
——只给标题、日期、片段，从不给全文，`IndexEntry.has_full_text` 恒为 `False`）。

`search_notice` 本身按日期区间查（比 Wind 精确），但实测有两个坑：
1. **没有公告 ID、没有链接。** `source_ref` 只能从 (实体, 日期, 标题) 摘要出来。
2. **同一份公告经常被命中两次**，标题措辞略有出入或完全相同——靠 (标题, 日期)
   去重。响应末尾还带一条没有标题/日期的免责声明对象，必须跳过而不是当成
   一条公告处理。

要用有会话状态的网关（`McpGatewayClient(..., sessions=True)`，Task 5）——iFind 的
MCP 服务器要求 `Mcp-Session-Id`，不带这个头会被当成新客户端处理。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.ingest.announcement_index import IndexEntry, known_at_for_day

PROVIDER = "ifind"
TOOL = "search_notice"


class IfindNoticeAdapter:
    provider = PROVIDER

    def __init__(self, gateway: GatewayClient) -> None:
        self._gateway = gateway

    def health(self) -> bool:
        return self._gateway.health()

    def fetch(self, ctx: FetchContext, *, short_name: str, since: date, until: date) -> RawResponse:
        args = {
            "query": f"{short_name}公告",
            "time_start": since.isoformat(),
            "time_end": until.isoformat(),
            "size": 20,
        }
        call = self._gateway.call_tool(TOOL, args)
        return replace(
            call.raw, provider=PROVIDER, endpoint=TOOL, params=args, payload=call.payload
        )

    def parse_entries(
        self, raw: RawResponse, *, entity_ref: str, short_names: Sequence[str]
    ) -> list[IndexEntry]:
        payload = raw.payload
        if not isinstance(payload, dict) or payload.get("error"):
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise AdapterError(f"iFind 公告查询返回错误：{error!r}")

        data = payload.get("data")
        try:
            items = json.loads(data) if isinstance(data, str) else (data or [])
        except json.JSONDecodeError as exc:
            raise AdapterError(f"iFind 公告响应 data 不是合法 JSON：{exc}") from exc

        seen: set[tuple[str, date]] = set()
        entries: list[IndexEntry] = []
        for item in items:
            title = str(item.get("公告标题") or "")
            raw_day = item.get("日期")
            if not title or not raw_day:
                continue  # 末尾的免责声明对象没有标题/日期，跳过而不是当成一条公告
            if not any(
                title.startswith(f"{n}:") or title.startswith(f"{n}：") for n in short_names
            ):
                continue
            try:
                day = date.fromisoformat(str(raw_day))
            except ValueError:
                continue
            key = (title, day)
            if key in seen:
                continue
            seen.add(key)

            source_ref = hashlib.sha256(f"{entity_ref}|{day}|{title}".encode()).hexdigest()[:16]
            entries.append(
                IndexEntry(
                    entity_ref=entity_ref,
                    source=PROVIDER,
                    source_ref=source_ref,
                    title=title,
                    publish_date=day,
                    known_at=known_at_for_day(day),
                    has_full_text=False,
                    excerpt=str(item.get("公告片段内容", "")) or None,
                )
            )
        return entries
