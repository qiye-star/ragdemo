import json
from typing import Any

import httpx
import pytest

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.parse.local.structure import ClosureFailed, LlmStructurer, StructureFailed

PAGE = "营业收入 91.2 亿元，上年同期 60.1 亿元。"


def _http(replies: list[str]) -> tuple[HttpClient, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        text = replies[min(len(seen) - 1, len(replies) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    client = HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=60000),
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def test_a_closed_reply_is_returned_and_usage_is_tallied() -> None:
    http, seen = _http(["# 经营情况\n\n营业收入 91.2 亿元，上年同期 60.1 亿元。"])
    s = LlmStructurer(http, model="deepseek-chat")
    out = s.structure(PAGE, [])
    assert out.startswith("# 经营情况") and "91.2" in out
    assert (s.usage.calls, s.usage.prompt_tokens, s.usage.completion_tokens) == (1, 100, 50)
    assert PAGE in seen[0]["messages"][0]["content"]
    assert seen[0]["temperature"] == 0


def test_an_invented_number_triggers_one_retry_naming_the_offenders() -> None:
    http, seen = _http(["收入 91.3 亿元", "收入 91.2 亿元，上年 60.1 亿元"])
    out = LlmStructurer(http, model="m").structure(PAGE, [])
    assert out == "收入 91.2 亿元，上年 60.1 亿元" and len(seen) == 2
    assert "91.3" in seen[1]["messages"][-1]["content"]  # 重试时点名违规数字


def test_still_invented_after_the_retry_raises_closure_failed() -> None:
    http, _ = _http(["收入 91.3", "收入 91.4"])
    with pytest.raises(ClosureFailed) as exc:
        LlmStructurer(http, model="m").structure(PAGE, [])
    assert exc.value.violations == ["91.4"]


def test_numbers_inside_the_provided_tables_count_as_source() -> None:
    table = "| 项目 | 2024 |\n|---|---|\n| 收入 | 91.2 |"
    http, _ = _http(["正文\n\n" + table])
    out = LlmStructurer(http, model="m").structure("正文", [table])
    assert "91.2" in out


def _http_raw(payloads: list[dict[str, Any]]) -> tuple[HttpClient, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=payloads[min(len(seen) - 1, len(payloads) - 1)])

    client = HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=60000),
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def _choice(content: str | None, finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_closure_failed_is_a_structure_failed() -> None:
    assert issubclass(ClosureFailed, StructureFailed)


def test_truncated_reply_raises_without_retry_and_tallies_usage() -> None:
    http, seen = _http_raw([_choice("# 半截 91.2", finish="length")])
    s = LlmStructurer(http, model="m")
    with pytest.raises(StructureFailed, match="截断"):
        s.structure(PAGE, [])
    assert len(seen) == 1 and s.usage.calls == 1 and s.usage.completion_tokens == 5


def test_max_tokens_is_configurable() -> None:
    http, seen = _http_raw([_choice("收入 91.2 上年 60.1")])
    LlmStructurer(http, model="m", max_tokens=1234).structure(PAGE, [])
    assert seen[0]["max_tokens"] == 1234


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_empty_or_null_content_raises_structure_failed(content: str | None) -> None:
    http, _ = _http_raw([_choice(content)])
    with pytest.raises(StructureFailed):
        LlmStructurer(http, model="m").structure(PAGE, [])


@pytest.mark.parametrize("payload", [{"choices": []}, {}, {"choices": [{}]}, {"choices": "x"}])
def test_malformed_choices_raise_structure_failed(payload: dict[str, Any]) -> None:
    http, _ = _http_raw([payload])
    with pytest.raises(StructureFailed):
        LlmStructurer(http, model="m").structure(PAGE, [])


def test_dropped_number_survives_retry_raises_structure_failed() -> None:
    http, seen = _http_raw([_choice("收入 91.2"), _choice("收入 91.2")])
    with pytest.raises(StructureFailed, match=r"60\.1") as exc:
        LlmStructurer(http, model="m").structure(PAGE, [])
    assert not isinstance(exc.value, ClosureFailed) and len(seen) == 2
    assert "60.1" in seen[1]["messages"][-1]["content"]


def test_dropped_number_restored_on_retry_returns_second_reply() -> None:
    full = "收入 91.2 上年 60.1"
    http, seen = _http_raw([_choice("收入 91.2"), _choice(full)])
    assert LlmStructurer(http, model="m").structure(PAGE, []) == full
    assert len(seen) == 2


def test_literal_placeholder_in_page_text_is_not_substituted() -> None:
    table = "| a | 1 |"
    page = "正文里有 {tables} 字样 1"
    http, seen = _http_raw([_choice(page + "\n" + table)])
    LlmStructurer(http, model="m").structure(page, [table])
    sent = seen[0]["messages"][0]["content"]
    assert sent.count("{tables}") == 1 and sent.count(table) == 1


_TPAGE = "项目 2024 2023 营业收入 91.2 60.1 净利润 19.3 12.7"
_TABLE = (
    "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 营业收入 | 91.2 | 60.1 |\n| 净利润 | 19.3 | 12.7 |"
)


def test_table_replacing_flat_text_is_not_reported_as_lost() -> None:
    http, seen = _http_raw([_choice(_TABLE)])
    assert LlmStructurer(http, model="m").structure(_TPAGE, [_TABLE]) == _TABLE
    assert len(seen) == 1


def test_flat_text_and_table_both_kept_is_allowed() -> None:
    reply = _TPAGE + "\n\n" + _TABLE
    http, seen = _http_raw([_choice(reply)])
    assert LlmStructurer(http, model="m").structure(_TPAGE, [_TABLE]) == reply
    assert len(seen) == 1


def test_table_with_dropped_rows_is_retried_then_fails() -> None:
    dropped = "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 营业收入 | 91.2 | 60.1 |"
    http, seen = _http_raw([_choice(dropped)])
    with pytest.raises(StructureFailed, match=r"19\.3"):
        LlmStructurer(http, model="m").structure(_TPAGE, [_TABLE])
    assert len(seen) == 2


def test_prompt_tells_model_to_replace_flat_table_text() -> None:
    http, seen = _http_raw([_choice(_TABLE)])
    LlmStructurer(http, model="m").structure(_TPAGE, [_TABLE])
    assert "替换" in seen[0]["messages"][0]["content"]
