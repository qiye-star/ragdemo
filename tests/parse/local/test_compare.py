from ragdemo.parse.local.compare import number_recall_precision, table_row_recall, text_coverage

REF_TABLE = "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 收入 | 91.2 | 60.1 |\n| 利润 | 19.3 | 12.7 |"


def test_text_coverage_is_character_recall_ignoring_whitespace() -> None:
    assert text_coverage("营业 收入", "营业收入增长") == 1.0
    assert text_coverage("营业收入", "营业") == 0.5
    assert text_coverage("", "x") == 1.0  # 参考为空：没有可召回的内容


def test_number_recall_and_precision_use_multisets() -> None:
    r, p = number_recall_precision("91.2 60.1 19.3", "91.2 60.1 12.7")
    assert round(r, 4) == round(2 / 3, 4) and round(p, 4) == round(2 / 3, 4)
    assert number_recall_precision("", "") == (1.0, 1.0)


def test_table_row_recall_requires_the_numbers_of_a_row_to_stay_together() -> None:
    same = (
        "| 项目 | 2024 | 2023 |\n|---|---|---|\n"
        "| 营业收入 | 91.2 | 60.1 |\n| 净利润 | 19.3 | 12.7 |"
    )
    assert table_row_recall(REF_TABLE, same) == 1.0
    # 数字都在，但串了行（19.3 跑到收入行）：行召回必须下降——这正是闭合校验抓不到的错
    shuffled = (
        "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 收入 | 91.2 | 12.7 |\n| 利润 | 19.3 | 60.1 |"
    )
    assert table_row_recall(REF_TABLE, shuffled) == 0.0
    assert table_row_recall("没有表格", "x") == 1.0
