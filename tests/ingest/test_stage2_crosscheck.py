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
    "| 中国工商银行股份有限公司-华<br>泰柏瑞沪深 300  交易型开放式指<br>数证券投资基金"
    " | 其他 | 1.24% | 18,300,412 | 0 |\n"
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


# --- 财务事实 vs 报告原文 -------------------------------------------------------------

from ragdemo.ingest.stage2_crosscheck import FactCheck, find_report_value  # noqa: E402

REPORT_TABLE = [
    (
        1,
        "| 项目 | 本报告期 | 年初至报告期末 |\n|---|---|---|\n"
        "| 营业收入 | 4,025,739,773.97 | 9,489,974,831.12 |\n"
        "| 归属于上市公司股东的扣<br>除非经常性损益的净利润 | 726,691,836.97 | 1,816,705,304.64 |\n"
        "| 归属于上市公司股东的净利润 | 759,994,598.37 | 1,961,446,439.52 |",
    ),
    (
        8,
        "| 项目 | 2025 年前三季度 |\n|---|---|\n| 其中:营业收入 | 9,489,974,831.12 |\n"
        "| 研发费用 | 1,000,000,000.00 |",
    ),
]


def test_find_report_value_picks_the_cell_closest_to_the_fact() -> None:
    labels = ("营业收入", "其中:营业收入")
    value, page = find_report_value(REPORT_TABLE, labels, Decimal("9489970000")) or (None, None)
    assert value == Decimal("9489974831.12")  # 累计列而不是本报告期列
    assert page in (1, 8)


def test_find_report_value_matches_the_whole_row_label_only() -> None:
    """ "归属于上市公司股东的扣除非经常性损益的净利润"不能被当成"归属于上市公司股东的净利润"。"""
    labels = ("归属于上市公司股东的净利润",)
    value, _ = find_report_value(REPORT_TABLE, labels, Decimal("1961450000")) or (None, None)
    assert value == Decimal("1961446439.52")
    other, _ = find_report_value(REPORT_TABLE, labels, Decimal("1816700000")) or (None, None)
    assert other is not None and other != Decimal("1816705304.64")  # 扣非行没有被认作


def test_find_report_value_none_when_the_row_is_absent() -> None:
    assert find_report_value(REPORT_TABLE, ("合同负债",), Decimal(1)) is None


def _check(fact: str, report: str | None) -> FactCheck:
    return FactCheck(
        "CN.1",
        "2025Q3",
        "revenue_total",
        Decimal(fact),
        None if report is None else Decimal(report),
        "t",
        1,
    )


def test_fact_check_tolerance_covers_wind_rounding_but_not_a_real_discrepancy() -> None:
    assert _check("9489970000", "9489974831.12").matched  # Wind 亿元 4 位小数 ≈ ±5000 元
    assert not _check("9489970000", "9490000000").matched  # 差 3 万元：不是取整误差
    assert not _check("9489970000", None).matched  # 找不到 = 没核对上，不算通过
