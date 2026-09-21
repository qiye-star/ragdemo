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


# --- 以下是本任务在 brief 必测用例之外新增的回归测试 ---
#
# 附注类表格（如资产减值准备明细表）在真实财报里几乎必有一列字面叫「说明」
# （备注列）。MockTableDescriber 把表头原样列进描述，若解读词只按裸子串匹配，
# "...、说明，共 N 行。" 这种纯粹枚举列名的句子会被误判为解读而拦截——
# 这是对 brief 给出的 INTERPRETATION_PATTERNS 第一条 `(显示|说明|表明|反映|
# 意味着|体现出)` 的修正：只在这些词后面紧跟实词（构成"V+宾语"的论断）时才算
# 解读，紧跟顿号/逗号/句末（说明书上用作名词/列名）不算。


def test_neutral_column_name_is_not_rejected() -> None:
    """「说明」作为表格列名（附注表格的备注列）出现时，不是在下论断，不应拦截。"""
    text = "存货表，列为 项目、期末余额、说明，共 1 行。"
    assert validate_description(text) == text


def test_mock_describer_handles_table_with_notes_column() -> None:
    table = "| 项目 | 期末余额 | 说明 |\n| 存货跌价准备 | 120 | 计提比例调整 |"
    desc = MockTableDescriber().describe(table, title="附注", section_path="附注 > 存货")
    assert "说明" in desc
    assert "1 行" in desc


def test_interpretation_word_followed_by_predicate_is_still_rejected() -> None:
    """「说明」后面接实词、构成完整论断时，仍然要拦截——不能因为上面的放宽而漏判。"""
    with pytest.raises(DescriptionRejected):
        validate_description("本表说明公司经营情况良好。")
