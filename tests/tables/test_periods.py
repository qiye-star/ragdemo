from __future__ import annotations

import pytest

from ragdemo.tables.periods import PeriodRef, doc_period_of_title, parse_period_path, shift_year


@pytest.mark.parametrize(
    ("path", "doc", "want"),
    [
        (("项目", "2025年1-9月"), "2025Q3", PeriodRef("2025Q3", "ytd")),
        (("2024年1－9月",), "2025Q3", PeriodRef("2024Q3", "ytd")),
        (("2025年7-9月",), "2025Q3", PeriodRef("2025Q3", "single_quarter")),
        (("年初至报告期末",), "2025Q3", PeriodRef("2025Q3", "ytd")),
        (("本报告期",), "2025Q3", PeriodRef("2025Q3", "single_quarter")),
        (("本报告期",), "2026Q1", PeriodRef("2026Q1", "ytd")),
        (("上年同期",), "2026Q1", PeriodRef("2025Q1", "ytd")),
        (("年初至报告期末", "上年同期"), "2025Q3", PeriodRef("2024Q3", "ytd")),
        (("上年同期",), "2025Q3", PeriodRef("2024Q3", "single_quarter")),
        (("2025年9月30日",), "2025Q3", PeriodRef("2025Q3", "point")),
        (("2024年12月31日",), "2025Q3", PeriodRef("2024FY", "point")),
        (("上年度末",), "2025Q3", PeriodRef("2024FY", "point")),
        (("本报告期末",), "2025H1", PeriodRef("2025H1", "point")),
        (("本期发生额",), "2025FY", PeriodRef("2025FY", "ytd")),
        (("2025年度",), "2025FY", PeriodRef("2025FY", "ytd")),
        (("本报告期比上年同期增减变动幅度(%)",), "2025Q3", None),
        (("股东名称",), "2025Q3", None),
        # extras (conservative behaviour)
        (("期末余额",), "2025H1", PeriodRef("2025H1", "point")),
        (("期初余额",), "2025H1", PeriodRef("2024FY", "point")),
        (("上期金额",), "2025H1", PeriodRef("2024H1", "ytd")),
        (("2025年1-6月",), "2025H1", PeriodRef("2025H1", "ytd")),
        (("2025年 6月30日",), "2025H1", PeriodRef("2025H1", "point")),
        (("2025年4月30日",), "2025Q3", None),
        (("本报告期末", "上年度末"), "2025Q3", None),
        (("2025年1-9月", "上年同期"), "2025Q3", None),
        (("2025年1-9月", "2024年1-9月"), "2025Q3", None),
        (("第三季度",), "2025Q3", None),
        (("本期", "增减"), "2025Q3", None),
        ((), "2025Q3", None),
        (("本报告期",), "bogus", None),
    ],
)
def test_period_columns(path: tuple[str, ...], doc: str, want: PeriodRef | None) -> None:
    assert parse_period_path(path, doc_period=doc) == want


def test_shift_year() -> None:
    assert shift_year("2026Q1", -1) == "2025Q1"
    assert shift_year("2025FY", -1) == "2024FY"


def test_doc_period_of_title() -> None:
    assert doc_period_of_title("某公司2025年第三季度报告") == "2025Q3"
    assert doc_period_of_title("随便") is None


@pytest.mark.parametrize(
    ("path", "doc", "want"),
    [
        # B1 movement columns
        (("本期增加",), "2025FY", None),
        (("本期减少",), "2025FY", None),
        (("本期计提",), "2025FY", None),
        (("本期购买金额",), "2025FY", None),
        (("本期确认的股利收入",), "2025FY", None),
        (("本期", "增加"), "2025FY", None),
        (("本期数",), "2025FY", PeriodRef("2025FY", "ytd")),
        (("本期金额",), "2025FY", PeriodRef("2025FY", "ytd")),
        # B2
        (("上期期末余额",), "2025FY", None),
        (("上年期末余额",), "2025FY", None),
        (("上期期初余额",), "2025FY", None),
        (("上年同期期末余额",), "2025Q3", None),
        # B3
        (("2025年三季度累计",), "2025Q3", None),
        (("2025年第三季度累计",), "2025Q3", None),
        (("2025年第三季度末",), "2025Q3", None),
        (("2025年三季度末",), "2025Q3", None),
        (("2025年度末",), "2025FY", None),
        (("2025年半年度末",), "2025H1", None),
        (("2025年上半年末",), "2025H1", None),
        (("2025年1-9月末",), "2025Q3", None),
        (("2025年6月末",), "2025H1", None),
        # B4
        (("截至2025年6月30日止六个月期间",), "2025H1", None),
        (("截至2025年9月30日止九个月",), "2025Q3", None),
        (("截至2025年3月31日止年度",), "2025FY", None),
        # B5
        (("较上年同期",), "2025Q3", None),
        (("较上年同期增长",), "2025Q3", None),
        (("上年同期增长率",), "2025Q3", None),
        (("上年同期差额",), "2025Q3", None),
        (("本期", "增长"), "2025FY", None),
        (("期末余额", "占比"), "2025FY", None),
        (("期末余额", "毛利率"), "2025FY", None),
        (("期末余额", "比重"), "2025FY", None),
        # B6
        (("截至报告期末累计投入募集资金总额(2)",), "2025Q3", None),
        # D1
        (("本报告期(4-6月)",), "2025H1", None),
        (("本报告期(1-3月)",), "2025H1", None),
        (("本报告期(1-9月)",), "2025Q3", None),
        (("年初至报告期末(1-6月)",), "2025Q3", None),
        (("本报告期(7-9月)",), "2025Q3", PeriodRef("2025Q3", "single_quarter")),
        (("本报告期(1-6月)",), "2025H1", PeriodRef("2025H1", "ytd")),
        # E1
        (("2025年度", "上年度"), "2025FY", None),
        (("2025年9月30日", "本报告期"), "2025Q3", None),
        (("2025年", "本报告期"), "2025Q3", None),
        # prose / dotted dates are not period headers
        (("经公司法定代表人签名的2024年半年度报告全文",), "2024H1", None),
        (("合并", "2022.12.31/2022年度"), "2023FY", None),
        (("2025年1-9月(未经审计)",), "2025Q3", PeriodRef("2025Q3", "ytd")),
        # robustness
        (("２０２５年９月３０日",), "2025Q3", PeriodRef("2025Q3", "point")),
        (("2025年9<br>月30日",), "2025Q3", PeriodRef("2025Q3", "point")),
        (("2025年1-9月",), None, PeriodRef("2025Q3", "ytd")),
        (("本报告期",), None, None),
        (("2025年3月31日",), "2025Q1", PeriodRef("2025Q1", "point")),
    ],
)
def test_review_fixes(path: tuple[str, ...], doc: str | None, want: PeriodRef | None) -> None:
    assert parse_period_path(path, doc_period=doc) == want
