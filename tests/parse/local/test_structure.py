import json
from typing import Any

import httpx
import pytest

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.parse.local.structure import ClosureFailed, LlmStructurer

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
    http, seen = _http(["收入 91.3 亿元", "收入 91.2 亿元"])
    out = LlmStructurer(http, model="m").structure(PAGE, [])
    assert out == "收入 91.2 亿元" and len(seen) == 2
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
