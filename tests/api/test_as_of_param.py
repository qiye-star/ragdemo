"""require_as_of：as_of 的五种失败形态分开报，且校验先于任何 DB 调用。

这里直接调用 require_as_of 这个纯函数（不经过完整的 FastAPI 请求生命周期）：
它不接触连接，测试这一层不需要数据库，也不需要装配 app——"整个参数缺失"
这一分支由 FastAPI 自身的必填校验先一步拦下，要连着 app.py 的异常处理器
一起验证，属于 test_app_contract.py 的范围（Task 3），不在这里重复。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ragdemo.api.deps import require_as_of
from ragdemo.api.errors import ApiError, ErrorCode


def test_valid_as_of_parses_to_aware_datetime() -> None:
    parsed = require_as_of("2026-09-22T00:00:00Z")
    assert parsed == datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)


def test_empty_string_raises_as_of_required() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_as_of("")
    assert exc_info.value.status_code == 422
    assert exc_info.value.error_detail["error"]["code"] == ErrorCode.AS_OF_REQUIRED


def test_unparsable_value_raises_as_of_invalid() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_as_of("yesterday")
    assert exc_info.value.status_code == 422
    assert exc_info.value.error_detail["error"]["code"] == ErrorCode.AS_OF_INVALID


def test_naive_value_raises_as_of_naive() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_as_of("2026-09-22T00:00:00")
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_detail["error"]["code"] == ErrorCode.AS_OF_NAIVE


def test_plus_decoded_as_space_gets_actionable_error() -> None:
    """`as_of=2026-09-22T00:00:00+08:00` 经 query string 解码后 '+' 变成
    空格——命中这个具体形状时，错误提示必须点名 %2B，而不是让调用方对着
    一个看不出所以然的 ValueError 摸不着头脑。"""
    with pytest.raises(ApiError) as exc_info:
        require_as_of("2026-09-22T00:00:00 08:00")
    assert exc_info.value.status_code == 400
    detail = exc_info.value.error_detail["error"]
    assert detail["code"] == ErrorCode.AS_OF_PLUS_DECODED_AS_SPACE
    assert "%2B" in detail["hint"]


def test_error_response_body_is_wrapped_in_error_envelope() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_as_of("not-a-date")
    assert set(exc_info.value.error_detail.keys()) == {"error"}
    assert {"code", "message", "param"} <= set(exc_info.value.error_detail["error"].keys())
