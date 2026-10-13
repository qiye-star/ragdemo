from __future__ import annotations

from ragdemo.parse.local.recall_diag import (
    MissingNumbers,
    classify_missing_numbers,
    normalize_reference_numbers,
    number_recall_precision_normalised,
)


def test_missing_numbers_are_attributed_to_decor_tokenization_or_absent_text_layer() -> None:
    m = classify_missing_numbers(
        "91.2 3 2029 4% 77",
        "91.2",
        layer_text="收入 91.2 亿元（同比 (4%)）",
        stripped_text="第 3 页",
    )
    assert (m.reference_total, m.hit) == (5, 1)
    assert (m.decor_stripped, m.tokenization, m.not_in_text_layer, m.unexplained) == (1, 1, 2, 0)
    assert m.examples == ("4%",)
    assert m.recall == 0.2


def test_numbers_stripped_with_a_figure_region_are_their_own_category() -> None:
    m = classify_missing_numbers(
        "91.2 34.1% 3", "91.2", layer_text="91.2", stripped_text="第 3 页", figure_text="34.1% 2021"
    )
    assert (m.figure_stripped, m.decor_stripped, m.unexplained) == (1, 1, 0)


def test_a_number_the_text_layer_has_to_spare_but_the_output_lacks_is_unexplained() -> None:
    m = classify_missing_numbers("91.2 5", "91.2", layer_text="91.2 5", stripped_text="")
    assert m.unexplained == 1 and m.examples == ("5",)


def test_numbers_already_used_by_the_output_are_not_counted_as_spare() -> None:
    m = classify_missing_numbers("1 1 1", "1", layer_text="1", stripped_text="")
    assert m.unexplained == 0 and m.tokenization == 2


def test_plus_accumulates_and_empty_recall_is_one() -> None:
    a = MissingNumbers(3, 2, 1, 0, 0, 0, ("x",), figure_stripped=1)
    b = MissingNumbers(2, 2, 0, 0, 0, 0, ("y",))
    assert a.plus(b) == MissingNumbers(5, 4, 1, 0, 0, 0, ("x", "y"), figure_stripped=1)
    assert MissingNumbers().recall == 1.0


# ---------------------------------------------------------------- T4 评审修复（I3、I4）
def test_i3_a_digit_string_split_across_separate_runs_is_not_tokenization() -> None:
    # "77" 只能由文字层里两个独立的 "7" 拼出来，不是同一个数字被分词打散
    m = classify_missing_numbers("77", "", layer_text="1 7 7 0", stripped_text="")
    assert (m.tokenization, m.not_in_text_layer) == (0, 1)


def test_i3_spaced_separators_inside_one_number_are_still_tokenization() -> None:
    m = classify_missing_numbers("5223600", "", layer_text="5 , 223 , 600", stripped_text="")
    assert (m.tokenization, m.not_in_text_layer) == (1, 0)


def test_i3_digits_are_matched_per_page_not_across_the_whole_document() -> None:
    pages = ["7", "7"]
    m = classify_missing_numbers(
        "77", "", layer_text="\n".join(pages), stripped_text="", layer_pages=pages
    )
    assert (m.tokenization, m.not_in_text_layer) == (0, 1)
    one = classify_missing_numbers(
        "77", "", layer_text="77", stripped_text="", layer_pages=["77"]
    )  # 同页里原样有（只是输出没用上）：落在 spare 里，不是 tokenization
    assert one.unexplained == 1


def test_i3_a_dropped_body_number_is_not_hidden_by_the_same_number_in_a_header() -> None:
    m = classify_missing_numbers("5", "", layer_text="共 5 项", stripped_text="第 5 页")
    assert (m.unexplained, m.decor_stripped) == (1, 0) and m.examples == ("5",)


def test_i4_spaced_decimals_and_thousands_are_normalised_on_the_reference_side() -> None:
    assert (
        normalize_reference_numbers("同比 5 . 83 % 与 5 , 223 , 600 元")
        == "同比 5.83 % 与 5,223,600 元"
    )
    assert normalize_reference_numbers("第 3 页 2024 年 5 月") == "第 3 页 2024 年 5 月"  # 不误并
    assert normalize_reference_numbers("") == ""


def test_i4_a_spaced_decimal_reference_counts_once_and_matches_the_candidate() -> None:
    m = classify_missing_numbers("增长 5 . 83% 与 5 , 223 , 600", "5.83% 5,223,600", "", "")
    assert (m.reference_total, m.hit, m.recall) == (2, 2, 1.0)
    assert number_recall_precision_normalised("5 . 83", "5.83") == (1.0, 1.0)
