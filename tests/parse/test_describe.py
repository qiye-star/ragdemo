"""表格描述：只描述表里有什么，不做任何解读或数值推断。"""
from __future__ import annotations

import pytest

from ragdemo.parse.describe import (
    DescriptionRejected,
    MockTableDescriber,
    validate_description,
)

TABLE = (
    "| 业务分部 | 2024H1 收入(百万元) | 同比 |\n"
    "| 智能计算 | 12,340 | +58.2% |\n"
    "| 通信设备 | 8,120 | -3.1% |"
)


def test_mock_description_mentions_the_columns() -> None:
    desc = MockTableDescriber().describe(TABLE, title="三季报", section_path="第三节 > 分部收入")
    assert "业务分部" in desc
    assert "2 行" in desc


def test_description_is_deterministic() -> None:
    d = MockTableDescriber()
    assert d.describe(TABLE, title="t", section_path="s") == d.describe(
        TABLE, title="t", section_path="s"
    )


def test_interpretation_words_are_rejected() -> None:
    """描述句的作用是让表格能被语义命中，不是替读者下结论。"""
    for bad in ("显示公司业绩大幅改善", "说明增长强劲", "表明景气度回升", "预计将继续增长"):
        with pytest.raises(DescriptionRejected):
            validate_description(f"分部收入表，{bad}。")


def test_compliance_forbidden_words_are_rejected() -> None:
    with pytest.raises(DescriptionRejected):
        validate_description("分部收入表，建议买入。")


def test_plain_description_passes() -> None:
    text = "2024 年上半年分部收入表，含智能计算、通信设备 2 个分部的收入与同比增速。"
    assert validate_description(text) == text


def test_empty_description_is_rejected() -> None:
    with pytest.raises(DescriptionRejected):
        validate_description("   ")


def test_describer_exposes_prompt_version() -> None:
    """prompt 版本要进 tool_call_log，否则无法定位是哪个版本产出的描述。"""
    d = MockTableDescriber()
    assert d.prompt_version
    assert d.model
