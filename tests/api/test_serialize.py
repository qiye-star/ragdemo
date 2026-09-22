"""行值转换：object → 具体类型，纯函数，无需数据库。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ragdemo.api.serialize import (
    as_bbox,
    as_bool,
    as_datetime,
    as_float,
    as_int,
    as_optional_float,
    as_optional_int,
    as_optional_str,
    as_str,
    as_str_list,
)


def test_as_int_accepts_int() -> None:
    assert as_int(42) == 42


def test_as_int_accepts_decimal() -> None:
    assert as_int(Decimal("7")) == 7


def test_as_int_rejects_bool() -> None:
    with pytest.raises(TypeError):
        as_int(True)


def test_as_int_rejects_str() -> None:
    with pytest.raises(TypeError):
        as_int("42")


def test_as_str_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        as_str(42)


def test_as_optional_str_passes_through_none() -> None:
    assert as_optional_str(None) is None
    assert as_optional_str("x") == "x"


def test_as_optional_int_passes_through_none() -> None:
    assert as_optional_int(None) is None
    assert as_optional_int(7) == 7


def test_as_bool_rejects_int_that_is_not_bool() -> None:
    with pytest.raises(TypeError):
        as_bool(1)


def test_as_float_accepts_decimal() -> None:
    assert as_float(Decimal("0.96")) == pytest.approx(0.96)


def test_as_float_rejects_bool() -> None:
    with pytest.raises(TypeError):
        as_float(True)


def test_as_optional_float_passes_through_none() -> None:
    assert as_optional_float(None) is None


def test_as_datetime_rejects_non_datetime() -> None:
    with pytest.raises(TypeError):
        as_datetime("2026-09-22T00:00:00Z")


def test_as_datetime_accepts_datetime() -> None:
    dt = datetime(2026, 9, 22, tzinfo=UTC)
    assert as_datetime(dt) is dt


def test_bbox_none_maps_to_null_not_malformed() -> None:
    bbox, malformed = as_bbox(None)
    assert bbox is None
    assert malformed is False


def test_bbox_decimal_array_becomes_four_floats() -> None:
    raw = [Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4")]
    bbox, malformed = as_bbox(raw)
    assert bbox == pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert malformed is False


def test_bbox_with_three_elements_is_flagged_malformed() -> None:
    bbox, malformed = as_bbox([Decimal("0.1"), Decimal("0.2"), Decimal("0.3")])
    assert bbox is None
    assert malformed is True


def test_bbox_with_five_elements_is_flagged_malformed() -> None:
    bbox, malformed = as_bbox([Decimal(str(v)) for v in (0.1, 0.2, 0.3, 0.4, 0.5)])
    assert bbox is None
    assert malformed is True


def test_as_str_list_none_becomes_empty_list() -> None:
    assert as_str_list(None) == []


def test_as_str_list_converts_jsonb_array() -> None:
    assert as_str_list(["budget_exceeded", "page 3 truncated"]) == [
        "budget_exceeded",
        "page 3 truncated",
    ]


def test_as_str_list_rejects_non_list() -> None:
    with pytest.raises(TypeError):
        as_str_list("not-a-list")
