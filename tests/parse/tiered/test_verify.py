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

from ragdemo.parse.tiered.verify import (  # noqa: E402
    VERIFY_VERSION,
    markup_violations,
    table_structure_violations,
)

TABLE_PAGE = "项目 期末余额 期初余额 营业收入 91.2 60.1 净利润 19.3 12.7 单位：亿元"


def _fails(md: str, page: str) -> bool:
    return not verify_against_text_layer(md, page).ok


def test_version_is_bumped() -> None:
    assert VERIFY_VERSION == "v6"


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


# ---- fix round 2 -------------------------------------------------------------------------

FILLER = "经营情况总体保持稳定" * 19  # ~190 字


def _h(rows: list[list[str]]) -> str:
    return (
        "<table>"
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        + "</table>"
    )


def test_c1_swapped_labels_in_a_table_fail() -> None:
    page = "营业收入 91.2 营业成本 60.1"
    assert _fails(_h([["营业成本", "91.2"], ["营业收入", "60.1"]]), page)
    assert verify_against_text_layer(_h([["营业收入", "91.2"], ["营业成本", "60.1"]]), page).ok


def test_c1_swapped_period_headers_fail() -> None:
    page = "项目 本期金额 上期金额 营业收入 91.2 60.1"
    assert _fails("项目 上期金额 本期金额 营业收入 91.2 60.1", page)


def test_c1_swapped_entity_names_fail() -> None:
    assert _fails("合并 91.2 母公司 60.1", "母公司 91.2 合并 60.1")


@pytest.mark.parametrize(
    ("page", "bad"),
    [
        ("甲公司未发生诉讼，乙公司发生诉讼", "甲公司发生诉讼，乙公司未发生诉讼"),
        ("收入增加，成本持平", "收入持平，成本增加"),
        (FILLER + "利润高于预期。", FILLER + "利润低于预期。"),
        (FILLER + "本期没有重大诉讼。", FILLER + "本期有重大诉讼。"),
        (FILLER + "差距收窄。", FILLER + "差距扩大。"),
    ],
)
def test_c1_word_level_swaps_fail(page: str, bad: str) -> None:
    assert _fails(bad, page)


@pytest.mark.parametrize(
    ("page", "good"),
    [
        (
            "项目 2024 2023 营业收入 91.2 60.1",
            "| 项目 | 2024 | 2023 |\n|---|---|---|\n| 营业收入 | 91.2 | 60.1 |",
        ),
        ("项目 2024 营业收入 91.2", _h([["项目", "2024"], ["营业<br/>收入", "91.2"]])),
        ("经营情况 营业收入 91.2", "# 经营情况\n\n**营业收入** 91.2"),
        ("收入 1,234.5 亿元", "收入 1234.5 亿元"),
        ("利润 −3.5 亿元", "利润 -3.5 亿元"),
        ("收入 91.2 亿元；利润 19.3 亿元", "收入91.2亿元,\n利润  19.3 亿元."),
    ],
)
def test_c1_faithful_variants_still_pass(page: str, good: str) -> None:
    v = verify_against_text_layer(good, page)
    assert v.ok, v.reasons


C2_PAGE = "项目 2024 2023 营业收入 91.2 60.1"
C2_HEAD = "| 项目 | 2024 | 2023 |\n|---|---|---|\n"


@pytest.mark.parametrize(
    "md",
    [
        C2_HEAD + "| 营业收入 | | 91.2 |\n| | 60.1 | |",
        _h([["项目", "2024", "2023"], ["营业收入", "", "91.2"], ["", "60.1", ""]]),
        "<table><tr><td>项目</td><td>2024</td><td>2023</td></tr><tr><td>营业收入</td>"
        '<td colspan="2">91.2</td></tr><tr><td>60.1</td></tr></table>',
        '<table><tr><td rowspan="2">项目</td><td>2024</td><td>2023</td></tr>'
        "<tr><td>营业收入</td><td>91.2</td><td>60.1</td></tr></table>",
        C2_HEAD + "| 营业收入 | 91.2 | 60.1 | 7 |",
        C2_HEAD + "| 营业收入 | 91.2 |\n| 净利润 | 60.1 |",
    ],
)
def test_c2_empty_cells_spans_and_ragged_rows_fail_on_text_layer(md: str) -> None:
    page = C2_PAGE + (" 净利润" if "净利润" in md else "") + (" 7" if "| 7 |" in md else "")
    v = verify_against_text_layer(md, page)
    assert not v.ok
    assert any("表格" in r for r in v.reasons), v.reasons


def test_c2_a_span_of_one_is_not_a_span() -> None:
    md = '<table><tr><td colspan="1">项目</td><td rowspan="1">2024</td></tr></table>'
    assert verify_against_text_layer(md, "项目 2024").ok


@pytest.mark.parametrize(
    "md",
    ["收入 &#57&#49.2 亿元", "收入 &#x39&#x31.2 亿元", "91&period;2", "&minus;3.5", "5&percnt;"],
)
def test_i3_entities_html_unescape_would_decode_are_rejected(md: str) -> None:
    assert markup_violations(md)
    assert not verify_against_text_layer(md, "收入 91.2 亿元").ok
    assert not verify_dual(md, md).ok


def test_i3_escaped_literals_are_allowed() -> None:
    assert not markup_violations("a &lt;b&gt; &amp; &quot;c&quot;&nbsp;d R&D AT&T")


def test_i4_dual_differing_spans_fail() -> None:
    plain = _h([["项目", "2024"], ["营业收入", "91.2"]])
    row = plain.replace("<td>营业收入</td>", '<td rowspan="2">营业收入</td>')
    col = plain.replace("<td>2024</td>", '<td colspan="2">2024</td>')
    assert not verify_dual(plain, row).ok
    assert not verify_dual(plain, col).ok
    assert verify_dual(plain, plain.replace("<td>项目", '<td colspan="1">项目')).ok
    pipe = "| 项目 | 2024 |\n|---|---|\n| 营业收入 | 91.2 |"
    assert verify_dual(plain, pipe).ok


def test_known_limitations_block_is_documented() -> None:
    import ragdemo.parse.tiered.verify as m

    assert m.__doc__ and "已知局限" in m.__doc__ and "border" in m.__doc__


# ---- fix round 3 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("母公司 91.2 合并 60.1", "合并 91.2 母公司 60.1"),
        ("公司本期盈利状况良好，现金流量保持稳健", "现金流量保持稳健，公司本期盈利状况良好"),
        (_h([["母公司", "91.2"], ["合并", "60.1"]]), _h([["合并", "91.2"], ["母公司", "60.1"]])),
    ],
)
def test_dual_label_and_clause_swaps_fail(a: str, b: str) -> None:
    v = verify_dual(a, b)
    assert not v.ok and any("内容顺序" in r for r in v.reasons)


def test_dual_cosmetic_differences_still_pass() -> None:
    assert verify_dual(_pipe(BASE), _html(BASE)).ok
    assert verify_dual(
        "# 经营情况\n\n**营业收入** 91.2，利润 19.3。", "经营情况 营业收入91.2 利润19.3"
    ).ok
    assert verify_dual("收入 1,234.5 亿元", "收入 1234.5 亿元").ok


# ---- fix round 4 -------------------------------------------------------------------------

R4_PAGE = "项目 2024 2023 营业收入 91.2 60.1"
R4_HEAD = "| 项目 | 2024 | 2023 |@N@|---|---|---|@N@"
R4_ROW = "<tr><td>项目</td><td>2024</td><td>2023</td></tr>"


@pytest.mark.parametrize(
    "md",
    [
        R4_HEAD + "营业收入 91.2 | 60.1",  # A1 无前导竖线的行
        "项目 | 2024 | 2023@N@--|--|--@N@营业收入 | 91.2 | 60.1",  # A1 无前导竖线的表头
        R4_HEAD + "| 营业收入 @P@| 91.2 | 60.1 |",  # A2 转义竖线
        "<table>"
        + R4_ROW
        + '<tr><td>营业收入</td><td colspan="2" colspan="1">91.2</td></tr></table>',  # A3
        "<table>"
        + R4_ROW
        + '<tr><td>营业收入</td><td colspan="2">91.2</td></tr>',  # A4 缺 </table>
        '<table/><tr><td>营业收入</td><td colspan="2">91.2</td></tr>',  # A4 自闭合
        "<table><tr><td>项目<td>2024</td><td>2023</td></tr></table>",  # A4 td 不平衡
        "<table>" + R4_ROW + "营业收入<tr><td>91.2</td><td>60.1</td></tr></table>",  # A5
        "<table><tr>营业收入<td>91.2</td><td>60.1</td></tr></table>",  # A5
    ],
)
def test_r4_a_tables_that_render_differently_from_the_parse_are_rejected(md: str) -> None:
    md = md.replace("@N@", chr(10)).replace("@P@", chr(92))
    assert not verify_against_text_layer(md, R4_PAGE).ok
    assert markup_violations(md) or table_structure_violations(md)
    if markup_violations(md):
        assert not verify_dual(md, md).ok


def test_r4_a_balanced_nested_markup_is_still_allowed() -> None:
    md = "<table><thead><tr><th>甲</th></tr></thead><tbody><tr><td>1<br>2</td></tr></tbody></table>"
    assert not markup_violations(md)
    assert not markup_violations(R4_HEAD.replace("@N@", chr(10)) + "| 营业收入 | 91.2 | 60.1 |")


@pytest.mark.parametrize("minus", ["‑", "‒", "﹣", "﹘", "⁃", "➖"])
def test_r4_b_minus_variants_are_folded(minus: str) -> None:
    page = f"净利润 {minus}3.5"
    assert not verify_against_text_layer("净利润 3.5", page).ok
    assert not verify_dual("净利润 3.5", page).ok
    assert verify_against_text_layer("净利润 -3.5", page).ok
    assert verify_dual("净利润 -3.5", page).ok


@pytest.mark.parametrize(
    ("good", "bad"),
    [
        ("增幅 ≥ 5", "增幅 ≤ 5"),
        ("利润 ▲ 5", "利润 ▼ 5"),
        ("利润 △ 5", "利润 ▽ 5"),
        ("利润 ↑ 5", "利润 ↓ 5"),
        ("误差 ± 5", "误差 5"),
        ("误差 > 5", "误差 < 5"),
    ],
)
def test_r4_c_comparison_and_arrow_symbols_are_critical(good: str, bad: str) -> None:
    assert _fails(bad, good)
    assert not verify_dual(good, bad).ok
    assert verify_against_text_layer(good, good).ok


def test_r4_known_limitations_are_documented() -> None:
    import ragdemo.parse.tiered.verify as m

    assert m.__doc__ and "删除线" in m.__doc__ and "罗马数字" in m.__doc__


# ---- fix round 5 -------------------------------------------------------------------------

NL = chr(10)
R5_PAGE = "项目 2024 2023 营业收入 91.2 60.1 净利润 3.5 2.1"
R5_HEAD = NL.join(["| 项目 | 2024 | 2023 |", "|---|---|---|", "| 营业收入 | 91.2 | 60.1 |"])


@pytest.mark.parametrize(
    "tail",
    [
        NL.join(["净利润", "| 3.5 | 2.1 |"]),
        NL.join(["净利润", "3.5 | 2.1"]),
        NL.join(["净利润 | 3.5", "| 2.1 |"]),
        NL.join(["---", "净利润 | 3.5 | 2.1"]),
    ],
)
def test_r5_i1_a_pipe_table_ends_only_at_a_blank_line(tail: str) -> None:
    v = verify_against_text_layer(R5_HEAD + NL + tail, R5_PAGE)
    assert not v.ok


def test_r5_i1_faithful_pipe_tables_still_pass() -> None:
    ok_blank = R5_HEAD + NL + "| 净利润 | 3.5 | 2.1 |" + NL + NL + "说明 完毕"
    assert verify_against_text_layer(ok_blank, R5_PAGE + " 说明 完毕").ok
    no_pipe = R5_HEAD + NL + "净利润 | 3.5 | 2.1"
    assert verify_against_text_layer(no_pipe, R5_PAGE).ok
    assert verify_against_text_layer(R5_HEAD, "项目 2024 2023 营业收入 91.2 60.1").ok


def test_r5_i1_a_late_delimiter_like_row_is_a_row_not_skipped() -> None:
    md = NL.join([R5_HEAD, "|---|---|", "| 净利润 | 3.5 | 2.1 |"])
    assert not verify_against_text_layer(md, R5_PAGE).ok


@pytest.mark.parametrize(
    "inner",
    [
        NL.join(["| 项目 | 2024 | 2023 |", "|---|---|---|", "| 营业收入 | | 91.2 | 60.1 |"]),
        NL.join(["| 项目 | 2024 | 2023 |", "|---|---|---|", "| 营业收入 | 91.2 | 60.1 |"]),
        NL.join(["| 项目 | 2024 |", "|---|---|", "| 营业收入 | 91.2 |"]),
    ],
)
def test_r5_i2_blank_lines_inside_html_tables_are_rejected(inner: str) -> None:
    md = "<table><tr><td>" + NL + NL + inner + NL + NL + "</td></tr></table>"
    assert markup_violations(md)
    assert not verify_against_text_layer(md, R4_PAGE).ok
    assert not verify_dual(md, md).ok


def test_r5_i2_html_tables_without_blank_lines_pass() -> None:
    md = NL.join(
        [
            "<table>",
            "<tr><td>项目</td><td>2024</td></tr>",
            "<tr><td>收入</td><td>91.2</td></tr>",
            "</table>",
        ]
    )
    assert not markup_violations(md)
    assert verify_against_text_layer(md, "项目 2024 收入 91.2").ok


def _all_pd() -> list[str]:
    import sys
    import unicodedata

    return [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Pd"]


def test_r5_i3_every_dash_punctuation_and_modifier_minus_folds_to_the_sign() -> None:
    from ragdemo.parse.local.number_closure import numbers_in

    dashes = [*_all_pd(), chr(0x2D7), chr(0x2212)]
    assert len(dashes) > 20
    for d in dashes:
        assert numbers_in(f"净利润 {d}3.5") == {"-3.5": 1}, hex(ord(d))


@pytest.mark.parametrize("x", [0x2015, 0x2E3A, 0x2E3B, 0x2D7, 0xFE31, 0xFE32])
def test_r5_i3_probed_dashes_cannot_lose_the_sign(x: int) -> None:
    page = f"净利润 {chr(x)}3.5"
    assert not verify_against_text_layer("净利润 3.5", page).ok
    assert not verify_dual("净利润 3.5", page).ok
    assert verify_against_text_layer("净利润 -3.5", page).ok
    assert verify_dual("净利润 -3.5", page).ok


def test_r5_known_limitations_are_documented() -> None:
    import ragdemo.parse.tiered.verify as m

    assert m.__doc__ and "分隔行" in m.__doc__ and "U+2500" in m.__doc__
