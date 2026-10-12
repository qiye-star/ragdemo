from ragdemo.parse.tiered.verify import (
    Verdict,
    critical_char_violations,
    verify_against_text_layer,
    verify_dual,
)

PAGE = "2024年营业收入 91.2 亿元，同比增长 51.7%；净利润 19.3 亿元。"


def test_a_faithful_engine_output_passes() -> None:
    md = (
        "- 2024年营业收入 91.2 亿元，同比增长 51.7%；\n- 净利润 19.3 亿元。"  # 只改排版，不增删文字
    )
    assert verify_against_text_layer(md, PAGE) == Verdict(True, ())


def test_text_the_page_does_not_have_fails_the_reverse_coverage() -> None:
    v = verify_against_text_layer("# 经营情况\n\n" + PAGE, PAGE)  # 引擎多写了 4 个原文没有的字
    assert not v.ok and v.reasons[0].startswith("反向覆盖率")


def test_an_html_table_is_compared_by_its_text_not_its_markup() -> None:
    page = "项目 2024 2023 营业收入 91.2 60.1 净利润 19.3 12.7 以上为本期主要数据，单位：亿元。"
    md = (
        '<table border="1"><tr><td>项目</td><td>2024</td><td>2023</td></tr>'
        "<tr><td>营业收入</td><td>91.2</td><td>60.1</td></tr>"
        '<tr><td colspan="1">净利润</td><td>19.3</td><td>12.7</td></tr></table>\n\n'
        "以上为本期主要数据，单位：亿元。"
    )
    assert verify_against_text_layer(md, page).ok  # border="1" / colspan="1" 里的数字不得进入比较


def test_an_invented_number_is_reported_both_ways() -> None:
    v = verify_against_text_layer(PAGE.replace("19.3", "19.8"), PAGE)
    assert not v.ok
    assert "凭空数字：['19.8']" in v.reasons and "丢失数字：['19.3']" in v.reasons


def test_a_changed_unit_fails_even_when_every_number_matches() -> None:
    v = verify_against_text_layer(PAGE.replace("91.2 亿元", "91.2 万元"), PAGE)
    assert not v.ok
    assert "关键字新增：万" in v.reasons and "关键字丢失：亿" in v.reasons


def test_a_dropped_page_number_counts_as_a_lost_number() -> None:
    v = verify_against_text_layer(PAGE, PAGE + " 第 3 页")
    assert "丢失数字：['3']" in v.reasons


def test_empty_output_fails() -> None:
    assert verify_against_text_layer(" \n", PAGE) == Verdict(False, ("引擎输出为空",))


def test_critical_char_violations_only_reports_surplus_in_structured() -> None:
    assert critical_char_violations("亿亿万", "亿万") == ["亿"]
    assert critical_char_violations("亿", "亿亿万") == []


def test_a_flipped_direction_word_fails_even_when_numbers_and_coverage_are_close() -> None:
    long_page = "公司本期营业收入同比增长 51.7%，" + "经营情况总体保持稳定" * 20
    flipped = long_page.replace("增长", "下降")
    v = verify_against_text_layer(flipped, long_page)
    assert not v.ok and "关键字丢失：增" in v.reasons


A_HTML = (
    "<table><tr><td>项目</td><td>2024</td><td>2023</td></tr>"
    "<tr><td>营业收入</td><td>91.2</td><td>60.1</td></tr>"
    "<tr><td>净利润</td><td>19.3</td><td>12.7</td></tr></table>\n单位：亿元"
)
B_PIPE = (
    "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 营业收入 | 91.2 | 60.1 |\n"
    "| 净利润 | 19.3 | 12.7 |\n\n单位：亿元"
)


def test_two_engines_that_agree_pass_across_html_and_pipe_tables() -> None:
    assert verify_dual(A_HTML, B_PIPE) == Verdict(True, ())


def test_one_differing_digit_fails() -> None:
    v = verify_dual(A_HTML, B_PIPE.replace("60.1", "60.7"))
    assert not v.ok and v.reasons[0].startswith("数字不一致")


def test_rows_swapped_between_lines_fail_even_with_equal_number_multisets() -> None:
    swapped = (
        "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 营业收入 | 91.2 | 12.7 |\n"
        "| 净利润 | 19.3 | 60.1 |\n\n单位：亿元"
    )
    v = verify_dual(A_HTML, swapped)
    assert not v.ok and any(r.startswith("表格行不一致") for r in v.reasons)


def test_a_unit_conflict_between_engines_fails() -> None:
    v = verify_dual(A_HTML, B_PIPE.replace("亿元", "万元"))
    assert "关键字（单位/方向/中文数字）不一致" in v.reasons


def test_one_empty_side_fails() -> None:
    assert verify_dual(A_HTML, "") == Verdict(False, ("至少一路输出为空",))
