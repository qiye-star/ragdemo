from ragdemo.parse.local.number_closure import closure_violations, normalize_number, numbers_in


def test_normalization_handles_thousands_percent_and_parenthesized_negatives() -> None:
    assert normalize_number("1,234.50") == "1234.50"
    assert normalize_number("(1,234)") == "(1234)"
    assert normalize_number("-3.5%") == "-3.5%"
    assert normalize_number("12.3%") == "12.3%"


def test_numbers_are_a_multiset() -> None:
    assert numbers_in("收入 91.2 亿，上年 60.1，再次 91.2") == {"91.2": 2, "60.1": 1}


def test_structured_text_with_only_source_numbers_is_closed() -> None:
    src = "营业收入 91.2 60.1 净利润 19.3 12.7 增长 51.7%"
    out = "| 营业收入 | 91.2 | 60.1 |\n| 净利润 | 19.3 | 12.7 |\n同比增长 51.7%"
    assert closure_violations(out, src) == []


def test_an_invented_number_is_reported() -> None:
    assert closure_violations("收入 91.3", "收入 91.2") == ["91.3"]


def test_an_extra_occurrence_of_a_real_number_is_also_a_violation() -> None:
    # 源文里 91.2 只出现一次，整理后出现两次 = 凭空多出一个
    assert closure_violations("91.2 与 91.2", "收入 91.2") == ["91.2"]


def test_thousands_separator_differences_do_not_count() -> None:
    assert closure_violations("收入 1234", "收入 1,234") == []


def test_negative_percentage_cannot_pass_as_positive() -> None:
    # 模型不能把 (3.5%) 的负值改成正的 3.5%
    assert closure_violations("净利润 3.5%", "同比 (3.5%)") == ["3.5%"]


def test_parenthesized_percentage_stays_closed_with_same_source() -> None:
    assert closure_violations("同比 (3.5%)", "同比 (3.5%)") == []
    assert numbers_in("(3.5)%") == {"(3.5%)": 1}


def test_range_not_treated_as_negative() -> None:
    # "10-20" 中的 "-" 是范围符，不是负号
    assert closure_violations("-20", "10-20") == ["-20"]
    assert closure_violations("10-20", "10-20") == []


def test_dates_not_tokenized_as_negatives() -> None:
    # "2024-05-01" 分解为三个数字，"-" 不作为负号
    assert numbers_in("2024-05-01") == {"2024": 1, "05": 1, "01": 1}


def test_list_numbering_not_converted_to_negative() -> None:
    # "(1)" 列表项 1 不能匹配 "-1"
    assert closure_violations("-1", "(1) 说明") == ["-1"]
    assert closure_violations("(1) 说明", "(1) 说明") == []


def test_fullwidth_normalized() -> None:
    # 全宽数字、逗号、百分号归一化后闭合
    assert closure_violations("1234 12.5%", "１，２３４ １２．５％") == []


def test_minus_after_cjk_is_sign() -> None:
    # CJK 文本后的 "-" 是负号
    assert numbers_in("下降-3.5") == {"-3.5": 1}


def test_sign_flip_is_caught() -> None:
    # 符号反转被检测到
    assert closure_violations("-91.2", "91.2") == ["-91.2"]
