"""Wind 公告降级层（公告双源接入计划 Task 6，ADR-0012/0013 分工：A 股公告主源
是巨潮资讯网，Wind 只在巨潮不可用时顶上目录——只给标题、日期、链接、摘录，
从不给全文，`IndexEntry.has_full_text` 恒为 `False`）。

实测确认的坑（`get_company_announcements` 是自然语言召回，不是结构化查询）：
1. **按相关度召回，会混入同名/相似公司。** 问"海光信息2026年半年度报告"，
   结果里混进"海光科技""光庭信息""神州信息"等公司的半年报——标题前缀过滤
   （必须以该实体的简称 + "："/"：" 开头）是必须的，不是防御性编程。
2. **没有日期参数**，问句里只能带年月，召回窗口不精确，需要按 `since`/`until`
   再筛一遍。
3. **没有公告 ID**，`source_ref` 从链接里的 `#/NA/<hash>` 段落取；同一份公告
   偶尔被同一批结果命中两次，靠 (标题, 日期) 去重。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ragdemo.adapters.base import FetchContext, RawResponse
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import GatewayClient
from ragdemo.ingest.announcement_index import IndexEntry, known_at_for_day

PROVIDER = "wind"
TOOL = "get_company_announcements"
_LINK_FRAGMENT = re.compile(r"#/NA/([0-9A-Fa-f]+)")


class WindDocsAdapter:
    provider = PROVIDER

    def __init__(self, gateway: GatewayClient) -> None:
        self._gateway = gateway

    def health(self) -> bool:
        return self._gateway.health()

    def fetch_month(self, ctx: FetchContext, *, short_name: str, month: date) -> RawResponse:
        """问句模板固定为"{简称}{年}年{月}月公告"——实测按月问的日期最集中，
        不带日参数（Wind 不支持）。"""
        question = f"{short_name}{month.year}年{month.month}月公告"
        call = self._gateway.call_tool(TOOL, {"query": question, "top_k": 20})
        return replace(
            call.raw,
            provider=PROVIDER,
            endpoint=TOOL,
            params={"short_name": short_name, "month": month.isoformat(), "query": question},
            payload=call.payload,
        )

    def parse_entries(
        self,
        raw: RawResponse,
        *,
        entity_ref: str,
        short_names: Sequence[str],
        since: date,
        until: date,
    ) -> list[IndexEntry]:
        payload = raw.payload
        if not isinstance(payload, dict) or payload.get("error"):
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise AdapterError(f"Wind 公告查询返回错误：{error!r}")
        items = ((payload.get("data") or {}).get("items")) or []

        seen: set[tuple[str, date]] = set()
        entries: list[IndexEntry] = []
        for item in items:
            title = str(item.get("title", ""))
            if not any(
                title.startswith(f"{n}:") or title.startswith(f"{n}：") for n in short_names
            ):
                continue
            try:
                day = date.fromisoformat(str(item.get("date", "")))
            except ValueError:
                continue
            if not (since <= day <= until):
                continue
            key = (title, day)
            if key in seen:
                continue
            seen.add(key)

            url = str(item.get("url", "")) or None
            match = _LINK_FRAGMENT.search(url or "")
            source_ref = (
                match.group(1)
                if match
                else hashlib.sha256(f"{entity_ref}|{day}|{title}".encode()).hexdigest()[:16]
            )
            entries.append(
                IndexEntry(
                    entity_ref=entity_ref,
                    source=PROVIDER,
                    source_ref=source_ref,
                    title=title,
                    publish_date=day,
                    known_at=known_at_for_day(day),
                    has_full_text=False,
                    url=url,
                    excerpt=str(item.get("content", "")) or None,
                )
            )
        return entries
