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
        "<table><tr><td>项目</td><td>2024</td><td>2023</td></tr>"
        "<tr><td>营业收入</td><td>91.2</td><td>60.1</td></tr>"
        '<tr><td colspan="1">净利润</td><td>19.3</td><td>12.7</td></tr></table>\n\n'
        "以上为本期主要数据，单位：亿元。"
    )
    assert verify_against_text_layer(
        md, page
    ).ok  # colspan="1" 里的数字不得进入比较（border 等属性按 v2 一律拒绝）


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


# ---- fix round 1 -------------------------------------------------------------------------

import pytest  # noqa: E402

from ragdemo.parse.tiered.verify import VERIFY_VERSION, markup_violations  # noqa: E402

TABLE_PAGE = "项目 期末余额 期初余额 营业收入 91.2 60.1 净利润 19.3 12.7 单位：亿元"


def _fails(md: str, page: str) -> bool:
    return not verify_against_text_layer(md, page).ok


def test_version_is_bumped() -> None:
    assert VERIFY_VERSION == "v2"


@pytest.mark.parametrize(
    "md",
    [
        "项目 期末余额 期初余额 营业收入 60.1 91.2 净利润 19.3 12.7 单位：亿元",  # cells swapped
        "项目 期初余额 期末余额 营业收入 91.2 60.1 净利润 19.3 12.7 单位：亿元",  # headers swapped
        "项目 期末余额 期初余额 营业收入 91.2 60.1 净利润 12.7 19.3 单位：亿元",  # row swapped
    ],
)
def test_text_layer_rejects_wrong_position_content(md: str) -> None:
    assert _fails(md, TABLE_PAGE)


def test_units_swapped_between_two_numbers_fail() -> None:
    page = "收入 91.2 亿元，支出 5.3 万元。"
    assert _fails("收入 91.2 万元，支出 5.3 亿元。", page)


def test_minus_sign_moved_to_the_other_number_fails() -> None:
    assert _fails("甲 5 乙 -3", "甲 -5 乙 3")


def test_same_order_with_other_layout_still_passes() -> None:
    assert verify_against_text_layer(TABLE_PAGE.replace(" ", "\n"), TABLE_PAGE).ok


@pytest.mark.parametrize(
    "md",
    [
        '正文<span title="营业收入 999 亿元"></span>',
        "正文<!-- 净利润应为 888 亿元 -->",
        "正文<增长至 777 亿元>",
        "正文<script>1</script>",
        '<table border="1"><tr><td>甲</td></tr></table>',
        '<table><tr><td class="x">甲</td></tr></table>',
        '<table><tr><td colspan="a">甲</td></tr></table>',
        "正文&#57;&#57;",
    ],
)
def test_hidden_markup_is_rejected_in_both_paths(md: str) -> None:
    assert markup_violations(md)
    v = verify_against_text_layer(md, "正文")
    assert not v.ok and any("标记" in r for r in v.reasons)
    v2 = verify_dual(md, md)
    assert not v2.ok and any("标记" in r for r in v2.reasons)


def test_plain_allowed_markup_is_not_flagged() -> None:
    assert not markup_violations(
        '<TABLE><thead><tr><th colspan="2">甲</th></tr></thead><tbody><tr><td rowspan=2>1</td>'
        "<td>2<br/>3</td></tr></tbody></table>"
    )


@pytest.mark.parametrize(
    ("good", "bad"),
    [
        ("未发生重大诉讼", "发生重大诉讼"),
        ("同比增长 5%", "环比增长 5%"),
        ("以人民币计价", "以港币计价"),
        ("本期无减值", "本期有减值"),
        ("不属于关联方", "属于关联方"),
        ("期末余额 3", "期初余额 3"),
    ],
)
def test_negation_period_and_currency_words_are_critical(good: str, bad: str) -> None:
    assert _fails(bad, good)
    assert not verify_dual(good, bad).ok


# dual ---------------------------------------------------------------------------------


def _pipe(rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _html(rows: list[list[str]]) -> str:
    return (
        "<table>"
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        + "</table>"
    )


BASE = [["项目", "2024", "2023"], ["营业收入", "91.2", "60.1"], ["净利润", "19.3", "12.7"]]


def test_dual_identical_and_markup_style_variants_pass() -> None:
    assert verify_dual(_pipe(BASE), _pipe(BASE)).ok
    assert verify_dual(_pipe(BASE), _html(BASE)).ok
    spaced = _pipe(BASE).replace("| ", "|   ")
    assert verify_dual(spaced, _html(BASE)).ok


def test_dual_columns_swapped_in_every_row_fail() -> None:
    sw = [[r[0], r[2], r[1]] for r in BASE]
    assert not verify_dual(_pipe(BASE), _pipe(sw)).ok
    assert not verify_dual(_html(BASE), _pipe(sw)).ok


def test_dual_header_years_swapped_fail() -> None:
    sw = [["项目", "2023", "2024"], *BASE[1:]]
    assert not verify_dual(_pipe(BASE), _pipe(sw)).ok


def test_dual_row_labels_swapped_fail() -> None:
    sw = [BASE[0], ["净利润", *BASE[1][1:]], ["营业收入", *BASE[2][1:]]]
    v = verify_dual(_pipe(BASE), _pipe(sw))
    assert not v.ok and any(r.startswith("表格行不一致") for r in v.reasons)


def test_dual_two_column_values_swapped_between_rows_fail() -> None:
    a = [["项目", "金额"], ["收入", "91.2"], ["利润", "19.3"]]
    b = [["项目", "金额"], ["收入", "19.3"], ["利润", "91.2"]]
    assert not verify_dual(_pipe(a), _pipe(b)).ok
    assert not verify_dual(_html(a), _pipe(b)).ok


def test_dual_swapped_units_in_prose_fail() -> None:
    assert not verify_dual("收入 91.2 亿元，支出 5.3 万元。", "收入 91.2 万元，支出 5.3 亿元。").ok


def test_dual_docstring_states_the_identical_error_limitation() -> None:
    assert verify_dual.__doc__ and "相同" in verify_dual.__doc__
