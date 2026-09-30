"""真实表格描述器（阶段 3.1）：请求形状、提示词版本、拦截后重试、拦截规则不放宽。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from ragdemo.config import load_config
from ragdemo.parse.describe import DescriptionRejected, MockTableDescriber
from ragdemo.parse.describe_llm import LlmTableDescriber, load_prompt
from ragdemo.parse.describer_factory import build_describer

TABLE = "| 项目 | 本期 | 上期 |\n|---|---|---|\n| 营业收入 | 1 | 2 |\n| 净利润 | 3 | 4 |"


def _client(replies: list[str], seen: list[dict[str, Any]]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        text = replies[min(len(seen) - 1, len(replies) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    return HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )


def _describer(replies: list[str], seen: list[dict[str, Any]], **kw: Any) -> LlmTableDescriber:  # noqa: ANN401
    return LlmTableDescriber(_client(replies, seen), model="deepseek-chat", **kw)


def test_request_shape_and_prompt_contents() -> None:
    seen: list[dict[str, Any]] = []
    d = _describer(["主要会计数据表，列为项目、本期、上期，共 2 行。"], seen)
    out = d.describe(
        TABLE, title="海光信息2025年第三季度报告", section_path="第二节 > 主要会计数据"
    )
    assert out == "主要会计数据表，列为项目、本期、上期，共 2 行。"
    (body,) = seen
    assert body["model"] == "deepseek-chat" and body["temperature"] == 0
    prompt = body["messages"][0]["content"]
    assert "海光信息2025年第三季度报告" in prompt and "第二节 > 主要会计数据" in prompt
    assert "营业收入" in prompt and "{table_markdown}" not in prompt
    assert d.prompt_version == "v3" and d.usage.calls == 1 and d.usage.prompt_tokens == 100


def test_prompt_file_versions_load_without_front_matter() -> None:
    for version in ("v1", "v2", "v3"):
        text = load_prompt(version)
        assert not text.startswith("---") and "{table_markdown}" in text
    assert "增持数量" in load_prompt("v2")  # v2 教模型用中性说法改写，而不是放宽拦截
    assert "显示、说明、表明" in load_prompt("v3")  # v3 明令避开解读动词


def test_only_the_first_non_empty_line_is_used_and_quotes_are_stripped() -> None:
    seen: list[dict[str, Any]] = []
    d = _describer(['\n"利润表主要科目表，共 2 行。"\n（说明：以上……）'], seen)
    assert d.describe(TABLE, title="t", section_path="s") == "利润表主要科目表，共 2 行。"


def test_a_blocked_reply_is_retried_with_the_reason_and_can_then_pass() -> None:
    seen: list[dict[str, Any]] = []
    d = _describer(["股东增持数量表，共 2 行。", "股份变动情况表，共 2 行。"], seen)
    assert d.describe(TABLE, title="t", section_path="s") == "股份变动情况表，共 2 行。"
    assert len(seen) == 2 and d.usage.rejected_attempts == 1
    retry_messages = seen[1]["messages"]
    assert retry_messages[1] == {"role": "assistant", "content": "股东增持数量表，共 2 行。"}
    assert "违禁词" in retry_messages[2]["content"]  # 拒绝原因回给了模型


def test_the_filter_is_not_relaxed_persistent_violations_raise() -> None:
    seen: list[dict[str, Any]] = []
    d = _describer(["业绩显示公司大幅改善。"], seen, max_attempts=3)
    with pytest.raises(DescriptionRejected, match="3 次输出都被拦截"):
        d.describe(TABLE, title="t", section_path="s")
    assert len(seen) == 3


def test_a_non_table_is_refused_before_any_request() -> None:
    seen: list[dict[str, Any]] = []
    with pytest.raises(DescriptionRejected):
        _describer(["x"], seen).describe("这不是表格", title="t", section_path="s")
    assert seen == []


def test_long_tables_are_truncated_in_the_prompt_but_the_row_count_is_kept() -> None:
    rows = "\n".join(f"| 科目{i} | {i} |" for i in range(200))
    table = f"| 项目 | 数 |\n|---|---|\n{rows}"
    seen: list[dict[str, Any]] = []
    _describer(["科目表，共 200 行。"], seen).describe(table, title="t", section_path="s")
    prompt = seen[0]["messages"][0]["content"]
    assert "科目0 " in prompt and "科目199" not in prompt
    assert "共 200 行正文" in prompt


def test_malformed_model_response_is_rejected_not_crashed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "x"})

    client = HttpClient(
        "deepseek",
        "http://127.0.0.1:8086",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0),
        bucket=TokenBucket(rate_per_minute=6000),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(DescriptionRejected, match="缺少 choices"):
        LlmTableDescriber(client, model="m").describe(TABLE, title="t", section_path="s")


def test_factory_uses_mock_without_a_model_url_and_llm_with_one() -> None:
    assert isinstance(build_describer(load_config({})), MockTableDescriber)
    described = build_describer(load_config({"MODEL_BASE_URL": "http://127.0.0.1:8086"}))
    assert isinstance(described, LlmTableDescriber)
    assert described.model == "deepseek-chat" and described.prompt_version == "v3"
    custom = build_describer(
        load_config(
            {
                "MODEL_BASE_URL": "http://x",
                "MODEL_NAME": "deepseek-reasoner",
                "DESCRIBE_PROMPT_VERSION": "v1",
            }
        )
    )
    assert isinstance(custom, LlmTableDescriber)
    assert custom.model == "deepseek-reasoner" and custom.prompt_version == "v1"


def test_retry_message_names_the_offending_wording_and_covers_a_column_called_shuoming() -> None:
    """真实公告的"非经常性损益项目 | 本期金额 | 说明"表：模型总把"说明列"写进描述而被拦截。
    重试提示必须点出触发拦截的具体字样，并教它"同名列不提"。"""
    seen: list[dict[str, Any]] = []
    d = _describer(
        [
            "非经常性损益项目表，按项目、本期金额和说明列划分，共 24 行。",
            "非经常性损益项目表，含项目与本期金额两列，共 24 行。",
        ],
        seen,
    )
    out = d.describe(TABLE, title="t", section_path="s")
    assert out == "非经常性损益项目表，含项目与本期金额两列，共 24 行。"
    retry = seen[1]["messages"][2]["content"]
    assert "“说明列”" in retry and "不要出现" in retry and "同名的列" in retry
