from ragdemo.parse.local.compare import (
    _table_rows,
    number_recall_precision,
    plain_text_from_markdown,
    table_row_recall,
    text_coverage,
)
from ragdemo.parse.local.number_closure import numbers_in

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


HTML_REF = (
    '<table border="1"><tr><td colspan="2">项目</td><td>2024</td></tr>'
    "<tr><td>收入</td><td>91.2</td><td>60.1</td></tr></table>"
)
PIPE_PERFECT = "| 项目 | 2024 |\n|---|---|\n| 收入 | 91.2 | 60.1 |"


def test_html_reference_rows_are_compared_against_a_pipe_candidate() -> None:
    assert table_row_recall(HTML_REF, PIPE_PERFECT) == 1.0
    swapped = "| 项目 | 2024 |\n|---|---|\n| 收入 | 91.2 | 99.9 |"
    assert table_row_recall(HTML_REF, swapped) == 0.0


def test_html_header_row_is_skipped_and_multi_table_reference() -> None:
    two = HTML_REF + "<p>x</p>" + HTML_REF.replace("91.2", "11.1")
    assert len(_table_rows(two)) == 2
    assert len(_table_rows(HTML_REF)) == 1


def test_plain_text_drops_tags_attributes_and_comments() -> None:
    from collections import Counter

    plain = plain_text_from_markdown("<!-- 页眉 2024 -->" + HTML_REF + "A&amp;B<br/>x")
    assert numbers_in(plain) == Counter({"2024": 1, "91.2": 1, "60.1": 1})
    assert "页眉" not in plain and "colspan" not in plain and "<" not in plain
    assert "A&B" in plain


def test_text_coverage_of_plain_text_against_perfect_candidate() -> None:
    cand = "项目 2024 收入 91.2 60.1"
    assert text_coverage(plain_text_from_markdown(HTML_REF), cand) >= 0.99


def test_heading_recall_precision_matches_on_normalized_text_not_level() -> None:
    from ragdemo.parse.local.compare import heading_recall_precision

    ref = "# 一、公司 概况\n\n正文\n\n## 二，经营情况\n\n### 未出现的标题\n"
    cand = "## 一、公司概况\n\n正文\n\n# 二、经营情况\n\n# 多出来的\n"
    recall, precision = heading_recall_precision(cand, ref)
    assert recall == 2 / 3 and precision == 2 / 3


def test_heading_extraction_ignores_html_tags_and_hash_in_text() -> None:
    from ragdemo.parse.local.compare import heading_matches

    ref = "# <b>第一节</b> 释义\n行内 # 不是标题\n<table><tr><td># x</td></tr></table>\n"
    m = heading_matches("# 第一节 释义\n", ref)
    assert m.matched == 1 and m.ref_total == 1 and m.cand_total == 1


def test_heading_level_agreement_counts_only_matched_headings() -> None:
    from ragdemo.parse.local.compare import heading_level_agreement

    ref = "# 甲\n## 乙\n### 丙\n"
    cand = "# 甲\n# 乙\n### 丙\n## 丁\n"
    assert heading_level_agreement(cand, ref) == 2 / 3


def test_heading_metrics_with_no_headings_are_vacuously_one() -> None:
    from ragdemo.parse.local.compare import heading_level_agreement, heading_recall_precision

    assert heading_recall_precision("正文", "正文") == (1.0, 1.0)
    assert heading_level_agreement("正文", "# 甲") == 1.0


def test_duplicate_headings_match_as_a_multiset() -> None:
    from ragdemo.parse.local.compare import heading_matches

    m = heading_matches("# 风险提示\n# 风险提示\n", "# 风险提示\n")
    assert m.matched == 1 and m.cand_total == 2
