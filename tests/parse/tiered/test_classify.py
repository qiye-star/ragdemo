from ragdemo.parse.tiered.classify import is_financial_statement_page

ROWS = "\n".join(f"科目{i} {i}.5 {i}.7" for i in range(1, 12))  # 11 行 × 3 个数字 = 33 个


def test_a_statement_page_is_detected() -> None:
    assert is_financial_statement_page("合并资产负债表\n项目 期末余额 期初余额\n" + ROWS)


def test_spaced_titles_are_detected() -> None:
    assert is_financial_statement_page("合 并 利 润 表\n" + ROWS)


def test_a_passing_mention_in_body_text_is_not_a_statement_page() -> None:
    assert not is_financial_statement_page("详见合并资产负债表附注 3，本期无重大变化。")


def test_a_dense_table_without_a_statement_title_is_not_a_statement_page() -> None:
    assert not is_financial_statement_page("主要经营数据\n" + ROWS)


def _nums(n: int) -> str:
    return " ".join(f"{i}.5" for i in range(1, n + 1))


def test_extra_titles_cover_traditional_english_and_other_names() -> None:
    for title in (
        "损益表",
        "综合收益表",
        "合併資產負債表",
        "合併利潤表",
        "合併現金流量表",
        "CONSOLIDATED BALANCE SHEET",
        "Statement of Cash Flows",
        "Statements of Cash Flows",
        "Statement of Comprehensive Income",
        "Income  Statement",
    ):
        assert is_financial_statement_page(title + "\n" + _nums(20)), title


def test_title_rule_edge_is_twenty_numbers() -> None:
    assert not is_financial_statement_page("合并利润表\n" + _nums(19))
    assert is_financial_statement_page("合并利润表\n" + _nums(20))


def test_two_row_label_markers_trigger_without_a_title() -> None:
    body = "流动资产合计 负债和所有者权益总计 "
    assert is_financial_statement_page(body + _nums(20))
    assert not is_financial_statement_page(body + _nums(19))
    assert is_financial_statement_page("期末余额 期初余额 " + _nums(20))
    assert is_financial_statement_page("本 期 金 额 上 期 金 额 " + _nums(20))


def test_one_marker_alone_is_not_enough() -> None:
    assert not is_financial_statement_page("经营活动产生的现金流量净额 " + _nums(30))
    assert not is_financial_statement_page("期末余额 期末余额 " + _nums(30))
