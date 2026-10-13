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
