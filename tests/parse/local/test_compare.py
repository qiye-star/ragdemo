from ragdemo.parse.local.compare import (
    _table_rows,
    heading_matches_v2,
    number_recall_precision,
    plain_text_from_markdown,
    table_data_rows,
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


def test_two_split_title_lines_match_one_merged_reference_title() -> None:
    cand = "# 海光信息技术股份有限公司\n# 关于2022年度业绩预告的公告\n## 一、本期业绩预告情况"
    ref = "# 海光信息技术股份有限公司关于 2022 年度业绩预告的公告\n## 一、 本期业绩预告情况"
    m = heading_matches_v2(cand, ref)
    assert (m.matched, m.cand_total, m.ref_total) == (2, 2, 2)
    assert m.recall == m.precision == 1.0 and m.relative_level_agreement == 1.0


def test_relative_level_agreement_ignores_absolute_depth() -> None:
    assert (
        heading_matches_v2(
            "# A\n## B\n## C\n# D", "## A\n### B\n### C\n## D"
        ).relative_level_agreement
        == 1.0
    )
    m = heading_matches_v2("# A\n# B", "# A\n## B")
    assert (m.rel_agree, m.rel_total) == (0, 1)


def test_unmatched_lines_are_not_merged_when_either_line_matches_alone() -> None:
    m = heading_matches_v2("# 甲\n# 乙", "# 甲乙\n# 甲")
    assert (m.matched, m.cand_total) == (1, 2)


def test_v2_metrics_with_no_headings_are_vacuously_one() -> None:
    m = heading_matches_v2("正文", "正文")
    assert m.recall == m.precision == m.relative_level_agreement == 1.0


def test_table_data_rows_is_the_public_row_extractor() -> None:
    md = "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 收入 | 91.2 | 60.1 |"
    assert [dict(r) for r in table_data_rows(md)] == [{"91.2": 1, "60.1": 1}]


def test_sum_heading_matches_adds_every_counter() -> None:
    from ragdemo.parse.local.compare import HeadingMatchV2, sum_heading_matches

    total = sum_heading_matches(
        [
            HeadingMatchV2(2, 3, 4, 1, 2),
            HeadingMatchV2(1, 1, 5, 0, 0),
            HeadingMatchV2(0, 0, 0, 0, 0),
        ]
    )
    assert total == HeadingMatchV2(3, 4, 9, 1, 2)
    assert sum_heading_matches([]) == HeadingMatchV2(0, 0, 0, 0, 0)


def test_wilson_interval_matches_the_known_value_and_is_degenerate_for_empty_samples() -> None:
    from ragdemo.parse.local.compare import wilson_interval

    lo, hi = wilson_interval(67, 109)
    assert (round(lo, 2), round(hi, 2)) == (0.52, 0.70)
    assert wilson_interval(0, 0) == (0.0, 1.0)
