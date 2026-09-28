"""iFind 公告降级层（公告双源接入计划 Task 6）：只给标题、日期、片段，没有链接、
没有公告 ID。夹具是 2026-09-28 对海光信息（688041.SH）`search_notice` 的真实响应。

实测确认的坑：同一份公告经常被命中两次，标题略有出入（"关于董事会换届选举的公告"
vs 同名不同措辞），响应末尾还带一条没有标题/日期的免责声明对象——都要能正确处理。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.ifind_notice import IfindNoticeAdapter
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.ingest.announcement_index import known_at_for_day

FIXTURE = Path("tests/fixtures/ifind/notice_688041.json")
SHORT_NAMES = ("海光信息", "海光")


def _payload() -> Any:  # noqa: ANN401
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _gateway(payload: Any) -> MockMcpGateway:  # noqa: ANN401
    return MockMcpGateway(
        tools=[ToolSpec("search_notice", "", {})], responses={"search_notice": payload}
    )


def _entries(payload: Any) -> list[Any]:  # noqa: ANN401
    adapter = IfindNoticeAdapter(_gateway(payload))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = adapter.fetch(ctx, short_name="海光信息", since=date(2026, 9, 1), until=date(2026, 9, 28))
    return list(adapter.parse_entries(raw, entity_ref="688041.SH", short_names=SHORT_NAMES))


def test_fetch_asks_the_expected_query_with_the_date_window() -> None:
    gateway = _gateway(_payload())
    adapter = IfindNoticeAdapter(gateway)
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    adapter.fetch(ctx, short_name="海光信息", since=date(2026, 9, 1), until=date(2026, 9, 28))
    (call,) = gateway.calls
    assert call[0] == "search_notice"
    assert call[1]["query"] == "海光信息公告"
    assert call[1]["time_start"] == "2026-09-01" and call[1]["time_end"] == "2026-09-28"


def test_ifind_snippet_becomes_an_excerpt_without_full_text() -> None:
    entries = _entries(_payload())
    assert entries and all(not e.has_full_text and e.excerpt for e in entries)
    assert all(e.source == "ifind" and e.entity_ref == "688041.SH" for e in entries)


def test_trailing_disclaimer_entry_without_a_title_is_skipped() -> None:
    entries = _entries(_payload())
    assert all(e.title for e in entries)


def test_duplicate_title_and_date_entries_collapse() -> None:
    entries = _entries(_payload())
    keys = [(e.title, e.publish_date) for e in entries]
    assert len(keys) == len(set(keys)), "夹具里同一份公告被命中两次，必须去重"


def test_items_for_other_companies_are_dropped() -> None:
    payload = _payload()
    items = json.loads(payload["data"])
    items.insert(0, {"公告标题": "中国平安：无关公告", "公告片段内容": "x", "日期": "2026-09-15"})
    payload["data"] = json.dumps(items, ensure_ascii=False)
    entries = _entries(payload)
    assert all(e.title.startswith(("海光信息", "海光")) for e in entries)


def test_known_at_is_the_conservative_day_end() -> None:
    entries = _entries(_payload())
    assert entries[0].known_at == known_at_for_day(entries[0].publish_date)
    assert entries[0].known_at.tzinfo is not None


def test_error_payload_raises_adapter_error() -> None:
    adapter = IfindNoticeAdapter(_gateway({"error": {"message": "无权限"}}))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = adapter.fetch(ctx, short_name="海光信息", since=date(2026, 9, 1), until=date(2026, 9, 28))
    with pytest.raises(AdapterError):
        list(adapter.parse_entries(raw, entity_ref="688041.SH", short_names=SHORT_NAMES))
