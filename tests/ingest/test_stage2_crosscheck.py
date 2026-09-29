"""股东交叉核对：解析表格里的断行 / 空格 / <br> 不能造成误报，数字对不上必须报出来。"""

from __future__ import annotations

from decimal import Decimal

from ragdemo.ingest.stage2_crosscheck import (
    holder_in_table,
    normalise_name,
    period_of_title,
    table_rows,
)

TABLE = [
    "| 中国工商银行股份有限公司-华<br>泰柏瑞沪深 300  交易型开放式指<br>数证券投资基金 | 其他 | 1.24% | 18,300,412 | 0 |\n"
    "| 香港中央结算有限公司 | 其他 | 2.5% | 73,126,741 | 0 |"
]


def test_name_normalisation_removes_br_spaces_and_hkscc_suffix() -> None:
    assert normalise_name("中国工商银行股份有限公司-华<br>泰柏瑞沪深 300  交易型") == (
        "中国工商银行股份有限公司-华泰柏瑞沪深300交易型"
    )
    assert normalise_name("香港中央结算有限公司(陆股通)") == "香港中央结算有限公司"


def test_broken_lines_in_the_parsed_table_do_not_cause_a_false_mismatch() -> None:
    rows = table_rows(TABLE)
    name = "中国工商银行股份有限公司-华泰柏瑞沪深300交易型开放式指数证券投资基金"
    assert holder_in_table(name, Decimal("18300412"), rows)
    assert holder_in_table("香港中央结算有限公司(陆股通)", Decimal("73126741"), rows)


def test_a_wrong_share_count_is_reported() -> None:
    rows = table_rows(TABLE)
    name = "中国工商银行股份有限公司-华泰柏瑞沪深300交易型开放式指数证券投资基金"
    assert not holder_in_table(name, Decimal("18300413"), rows)  # 差 1 股
    assert not holder_in_table("不存在的股东", Decimal("18300412"), rows)


def test_period_of_title_covers_both_quarter_spellings() -> None:
    assert period_of_title("海光信息技术股份有限公司2025年第三季度报告") == "2025Q3"
    assert period_of_title("2025年一季度报告") == "2025Q1"
    assert period_of_title("2025年半年度报告摘要") == "2025H1"
    assert period_of_title("2024年年度报告") == "2024FY"
    assert period_of_title("关于2024年年度报告的专项说明") is None
