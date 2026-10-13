from __future__ import annotations

from ragdemo.parse.local.recall_diag import MissingNumbers, classify_missing_numbers


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
