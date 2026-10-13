from decimal import Decimal

from ragdemo.tables.numbers import CellNumber, cell_decimals, cell_value, token_to_decimal


def test_tokens() -> None:
    assert token_to_decimal("1234.50") == (Decimal("1234.50"), False)
    assert token_to_decimal("(3,000)") == (Decimal("-3000"), False)
    assert token_to_decimal("-12.5%") == (Decimal("-12.5"), True)
    assert token_to_decimal("(3.5%)") == (Decimal("-3.5"), True)
    assert token_to_decimal("abc") is None


def test_cell_decimals_uses_number_closure_tokenizer() -> None:
    assert cell_decimals("1,234.50") == [(Decimal("1234.50"), False)]
    assert cell_decimals("（1,000）") == [(Decimal("-1000"), False)]
    assert cell_decimals("-") == []


def test_ambiguous_or_non_decimal_tokens_are_not_numbers() -> None:
    for bad in (
        "NaN", "Infinity", "-Infinity", "1e5", "1_000", "(-3)", "(3", "3)", "--5", "+5", "", "%",
        "(%)", "1.2.3", "１２",
    ):  # fmt: skip
        assert token_to_decimal(bad) is None, bad


def test_signs_percent_and_variants_through_cell_decimals() -> None:
    assert cell_decimals("−5") == [(Decimal("-5"), False)]  # Unicode minus
    assert cell_decimals("－5") == [(Decimal("-5"), False)]  # full-width hyphen-minus
    assert cell_decimals("（3.5）%") == [(Decimal("-3.5"), True)]
    assert cell_decimals("１２，３４５．６") == [(Decimal("12345.6"), False)]
    assert cell_decimals("-.5") == [(Decimal("-0.5"), False)]
    zero = token_to_decimal("0.00")
    assert zero is not None and zero[0].as_tuple().exponent == -2


def test_parenthesised_needs_decimal_or_thousands_structure() -> None:
    assert token_to_decimal("(3.50)") == (Decimal("-3.50"), False)
    assert token_to_decimal("(1)") is None
    assert token_to_decimal("(3000)") is None
    assert cell_decimals("1,234(1)") == [(Decimal("1234"), False)]
    assert cell_decimals("( 1 )重新计量设定受益计划变动额") == []


def test_cell_value_plain_amounts_percent_and_negatives() -> None:
    assert cell_value("1,234.50") == CellNumber(Decimal("1234.50"), "amount", None, "1,234.50")
    assert cell_value(" -12.5% ") == CellNumber(Decimal("-12.5"), "percent", None, "-12.5%")
    a = cell_value("(1,234)")
    assert a is not None and a.value == Decimal("-1234")
    b = cell_value("(3.50)")
    assert b is not None and b.value == Decimal("-3.50")
    c = cell_value("（3.5）%")
    assert c is not None and c.kind == "percent" and c.value == Decimal("-3.5")
    d = cell_value("−5")
    assert d is not None and d.value == Decimal("-5")


def test_cell_value_inline_unit_is_captured_not_dropped() -> None:
    cn = cell_value("29,285.7143 万美元")
    assert cn is not None and cn.value == Decimal("29285.7143") and cn.inline_unit == "万美元"
    cn = cell_value("100<br>万元")
    assert cn is not None and cn.inline_unit == "万元"


def test_cell_value_rejects_everything_ambiguous() -> None:
    for bad in (
        "649,900,00<br>0",  # digit<br>digit split
        "增加 16 . 70 个百分点",  # spaced decimal + sign word
        "减少 3.68 个百分点",  # sign word would flip the sign
        "增加3.5%", "下降 3.5%", "上升1", "2025年9月30日", "2025年", "1-9", "2025-09-30",
        "10 20", "1,234\n6.7", "(1)", "(3000)", "1,234(1)", "( 1 )重新计量设定受益计划变动额",
        "1,23", "12,3456", "- 5", "+5", "1e5", "NaN", "", "-", "--", "￥1,234",
        "12.5%万元", "3 个百分点",
    ):  # fmt: skip
        assert cell_value(bad) is None, bad
