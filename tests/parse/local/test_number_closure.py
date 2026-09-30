from ragdemo.parse.local.number_closure import closure_violations, normalize_number, numbers_in


def test_normalization_handles_thousands_percent_and_parenthesized_negatives() -> None:
    assert normalize_number("1,234.50") == "1234.50"
    assert normalize_number("(1,234)") == "-1234"
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
