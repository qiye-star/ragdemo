"""Wind 公告降级层（公告双源接入计划 Task 6）：巨潮不可用时顶上目录，只给摘录、
不给全文。夹具是 2026-09-28 对海光信息（688041.SH）的真实响应。

实测确认的坑：Wind 的公告召回按相关度排序，问"海光信息2026年半年度报告"会混入
"海光科技""光庭信息""神州信息"等同名/相似公司的半年报——标题前缀过滤是必须的，
不是防御性编程。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.errors import AdapterError
from ragdemo.adapters.mcp_gateway import ToolSpec
from ragdemo.adapters.mock.mcp_gateway import MockMcpGateway
from ragdemo.adapters.wind_docs import WindDocsAdapter

FIXTURE = Path("tests/fixtures/wind/announcements_688041.json")
SHORT_NAMES = ("海光信息", "海光")


def _payload() -> Any:  # noqa: ANN401
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _gateway(payload: Any) -> MockMcpGateway:  # noqa: ANN401
    return MockMcpGateway(
        tools=[ToolSpec("get_company_announcements", "", {})],
        responses={"get_company_announcements": payload},
    )


def test_fetch_month_asks_the_expected_question() -> None:
    gateway = _gateway(_payload())
    adapter = WindDocsAdapter(gateway)
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    adapter.fetch_month(ctx, short_name="海光信息", month=date(2026, 8, 1))
    (call,) = gateway.calls
    assert call[0] == "get_company_announcements"
    assert call[1]["query"] == "海光信息2026年8月公告"


def _entries(
    payload: Any,  # noqa: ANN401
    since: date = date(2026, 1, 1),
    until: date = date(2026, 12, 31),
) -> list[Any]:
    adapter = WindDocsAdapter(_gateway(payload))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = adapter.fetch_month(ctx, short_name="海光信息", month=date(2026, 8, 1))
    return list(
        adapter.parse_entries(
            raw, entity_ref="688041.SH", short_names=SHORT_NAMES, since=since, until=until
        )
    )


def test_wind_items_for_other_companies_are_dropped() -> None:
    entries = _entries(_payload())
    titles = {e.title for e in entries}
    assert all(t.startswith(("海光信息", "海光")) for t in titles)
    assert "海光科技:2026年半年度报告" not in titles
    assert "光庭信息:2026年半年度报告" not in titles
    assert any("海光信息" in t for t in titles)


def test_wind_entries_have_no_full_text_and_carry_an_excerpt() -> None:
    entries = _entries(_payload())
    assert entries and all(not e.has_full_text and e.excerpt for e in entries)
    assert all(e.source == "wind" and e.entity_ref == "688041.SH" for e in entries)


def test_wind_source_ref_comes_from_the_link_fragment() -> None:
    entries = _entries(_payload())
    real = next(e for e in entries if e.title == "海光信息:2026年半年度报告")
    assert real.source_ref and real.url and real.source_ref in real.url


def test_wind_items_outside_the_window_are_dropped() -> None:
    entries = _entries(_payload(), since=date(2026, 8, 14), until=date(2026, 8, 14))
    assert {e.publish_date for e in entries} == {date(2026, 8, 14)}


def test_wind_duplicate_items_collapse() -> None:
    payload = _payload()
    items = payload["data"]["items"]
    items.append(dict(items[0]))  # 同实体同日同标题再来一条
    entries = _entries(payload)
    keys = [(e.title, e.publish_date) for e in entries]
    assert len(keys) == len(set(keys))


def test_error_payload_raises_adapter_error() -> None:
    adapter = WindDocsAdapter(_gateway({"error": {"message": "额度不足"}}))
    ctx = FetchContext(ingest_run_id="t", partition_date=date(2026, 9, 28))
    raw = adapter.fetch_month(ctx, short_name="海光信息", month=date(2026, 8, 1))
    with pytest.raises(AdapterError):
        list(
            adapter.parse_entries(
                raw,
                entity_ref="688041.SH",
                short_names=SHORT_NAMES,
                since=date(2026, 1, 1),
                until=date(2026, 12, 31),
            )
        )


def test_known_at_is_the_conservative_day_end() -> None:
    from ragdemo.ingest.announcement_index import known_at_for_day

    entries = _entries(_payload())
    real = next(e for e in entries if e.title == "海光信息:2026年半年度报告")
    assert real.known_at == known_at_for_day(date(2026, 8, 14))
    assert real.known_at.tzinfo is not None
