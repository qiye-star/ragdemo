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
