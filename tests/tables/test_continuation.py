from __future__ import annotations

from ragdemo.tables.continuation import TableSlot, continuation_links

A = TableSlot(3, 4, 3, ("项目", "本期", "上期"), ("项目", "本期", "上期"), False, True, "L1")


def _b(**kw: object) -> TableSlot:
    base: dict[str, object] = dict(
        ordinal=4,
        page=5,
        n_cols=3,
        header_row=("项目", "本期", "上期"),
        first_row=("项目", "本期", "上期"),
        first_on_page=True,
        last_on_page=False,
        tier="L1",
    )
    base.update(kw)
    return TableSlot(**base)  # type: ignore[arg-type]


def test_repeated_header_links() -> None:
    assert continuation_links([A, _b()]) == {4: 3}


def test_body_row_continuation_links() -> None:
    assert continuation_links([A, _b(first_row=("应收账款", "1,000", "900"))]) == {4: 3}


def test_boundaries_do_not_link() -> None:
    assert continuation_links([A, _b(tier="L3")]) == {}
    assert continuation_links([A, _b(n_cols=4)]) == {}
    assert continuation_links([A, _b(page=6)]) == {}
    assert continuation_links([A, _b(first_on_page=False)]) == {}
    assert continuation_links([A, _b(first_row=("合并利润表", "", ""))]) == {}


def test_a_must_be_last_on_page_and_not_l3() -> None:
    a = TableSlot(3, 4, 3, A.header_row, A.first_row, False, False, "L1")
    assert continuation_links([a, _b()]) == {}
    a3 = TableSlot(3, 4, 3, A.header_row, A.first_row, False, True, "L3")
    assert continuation_links([a3, _b()]) == {}


def test_unknown_tier_none_may_link_but_is_not_l3() -> None:
    a = TableSlot(3, 4, 3, A.header_row, A.first_row, False, True, None)
    assert continuation_links([a, _b(tier=None)]) == {4: 3}


def test_blank_header_row_never_counts_as_repeated_header() -> None:
    blank = ("", "", "")
    a = TableSlot(3, 4, 3, blank, blank, False, True, "L1")
    assert continuation_links([a, _b(header_row=blank, first_row=blank)]) == {}


def test_chain_and_input_order_independent() -> None:
    mid = _b(ordinal=4, page=5, first_on_page=True, last_on_page=True)
    c = _b(ordinal=5, page=6)
    assert continuation_links([c, mid, A]) == {4: 3, 5: 4}


def test_non_adjacent_ordinals_do_not_link() -> None:
    assert continuation_links([A, _b(ordinal=6)]) == {}
