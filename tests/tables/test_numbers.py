from decimal import Decimal

from ragdemo.tables.numbers import cell_decimals, token_to_decimal


def test_tokens() -> None:
    assert token_to_decimal("1234.50") == (Decimal("1234.50"), False)
    assert token_to_decimal("(3000)") == (Decimal("-3000"), False)
    assert token_to_decimal("-12.5%") == (Decimal("-12.5"), True)
    assert token_to_decimal("(3.5%)") == (Decimal("-3.5"), True)
    assert token_to_decimal("abc") is None


def test_cell_decimals_uses_number_closure_tokenizer() -> None:
    assert cell_decimals("1,234.50") == [(Decimal("1234.50"), False)]
    assert cell_decimals("（1,000）") == [(Decimal("-1000"), False)]
    assert cell_decimals("-") == []


def test_ambiguous_or_non_decimal_tokens_are_not_numbers() -> None:
    for bad in (
        "NaN",
        "Infinity",
        "-Infinity",
        "1e5",
        "1_000",
        "(-3)",
        "(3",
        "3)",
        "--5",
        "+5",
        "",
        "%",
        "(%)",
        "1.2.3",
        "１２",
    ):
        assert token_to_decimal(bad) is None, bad


def test_signs_percent_and_variants_through_cell_decimals() -> None:
    assert cell_decimals("−5") == [(Decimal("-5"), False)]  # Unicode minus
    assert cell_decimals("－5") == [(Decimal("-5"), False)]  # full-width hyphen-minus
    assert cell_decimals("（3.5）%") == [(Decimal("-3.5"), True)]
    assert cell_decimals("１２，３４５．６") == [(Decimal("12345.6"), False)]
    assert cell_decimals("-.5") == [(Decimal("-0.5"), False)]
    assert token_to_decimal("0.00")[0].as_tuple().exponent == -2  # type: ignore[index]
