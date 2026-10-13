from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ragdemo.parse.tiered.cost_model import (
    BacklogDoc,
    CostComparison,
    CostEstimate,
    GroupEstimate,
    backlog_scenarios,
    compare_costs,
    estimate,
    l0_pages,
    plan_budget,
    unverified_rate,
)
from ragdemo.parse.tiered.ledger import TierRow

P = Decimal("0.1356")
WHOLE = [GroupEstimate("定期报告", 2074, Decimal(0)), GroupEstimate("holdout2", 74, Decimal(0))]


def test_the_plan_table_is_reproduced_conservative_and_optimistic() -> None:
    conservative = [
        *WHOLE,
        GroupEstimate("非定期公告", 4297, Decimal("0.3579")),
        GroupEstimate("研报", 596, Decimal("0.282")),
    ]
    optimistic = [
        *WHOLE,
        GroupEstimate("非定期公告", 4297, Decimal("0.564")),
        GroupEstimate("研报", 596, Decimal("0.3196")),
    ]
    assert estimate(conservative, price=P) == CostEstimate(
        7041, 5335, Decimal("723.4260"), Decimal("0.1027")
    )
    assert estimate(optimistic, price=P) == CostEstimate(
        7041, 4427, Decimal("600.3012"), Decimal("0.0853")
    )
    assert estimate([*WHOLE, GroupEstimate("全送", 4893, Decimal(0))], price=P).cny == Decimal(
        "954.7596"
    )


def test_l0_pages_round_half_up_and_shares_are_validated() -> None:
    assert l0_pages(GroupEstimate("x", 4297, Decimal("0.564"))) == 2424
    assert l0_pages(GroupEstimate("x", 596, Decimal("0.282"))) == 168
    with pytest.raises(ValueError):
        GroupEstimate("x", 10, Decimal("1.2"))
    with pytest.raises(ValueError):
        GroupEstimate("x", 10, Decimal("0.5"), skipped_pages=6)


def test_before_after_comparison_counts_cached_l3_pages_as_billed() -> None:
    rows = [
        TierRow("L0", 60, 0, Decimal(0), 0),
        TierRow("L3", 35, 30, P * 30, 0),
        TierRow("skipped", 5, 0, Decimal(0), 0),
    ]
    assert compare_costs(rows, price=P) == CostComparison(
        pages=100,
        billed_pages=30,
        unbilled_l3_pages=5,
        actual_cny=Decimal("4.7460"),
        counterfactual_cny=Decimal("13.5600"),
        actual_per_page=Decimal("0.0475"),
        saving_share=Decimal("0.6500"),
    )


def test_unverified_rate() -> None:
    assert unverified_rate(0, 120) == Decimal("0.0000")
    assert unverified_rate(3, 120) == Decimal("0.0250")
    assert unverified_rate(0, 0) is None


def _d(i: int, tier: str, pages: int, day: int) -> BacklogDoc:
    return BacklogDoc(i, tier, pages, datetime(2026, 1, day, tzinfo=UTC), f"t{i}")


DOCS = [
    _d(1, "second", 100, 5),
    _d(2, "none", 10, 1),
    _d(3, "none", 30, 9),
    _d(4, "first", 4, 2),
    _d(5, "none", 8, 9),  # same day as 3: ties broken by doc_id
    _d(6, "none", 500, 20),
]


def test_plan_default_priority_first_then_none_newest_first_then_second() -> None:
    plan = plan_budget(DOCS, budget_pages=60, price=P)
    # order: 4(first) | 6(none,500 too big) 3, 5, 2 | 1(second)
    assert [ln.doc.doc_id for ln in plan.selected] == [4, 3, 5, 2]
    assert [ln.cum_pages for ln in plan.selected] == [4, 34, 42, 52]
    assert plan.selected[-1].cum_cny == P * 52
    assert plan.used_pages == 52
    assert [d.doc_id for d in plan.remaining] == [6, 1]
    assert plan.remaining_pages == 600
    assert plan.remaining_cny == P * 600
    assert plan.budget_left_pages == 8


def test_plan_strict_order_stops_at_first_non_fitting_doc() -> None:
    plan = plan_budget(DOCS, budget_pages=60, price=P, strict_order=True)
    assert [ln.doc.doc_id for ln in plan.selected] == [4]
    assert plan.used_pages == 4
    assert plan.remaining_pages == 648


def test_plan_custom_priority_and_validation() -> None:
    plan = plan_budget(DOCS, budget_pages=120, price=P, priority=("second", "first", "none"))
    assert [ln.doc.doc_id for ln in plan.selected] == [1, 4, 5]
    with pytest.raises(ValueError):
        plan_budget(DOCS, budget_pages=10, price=P, priority=("first",))
    with pytest.raises(ValueError):
        plan_budget(DOCS, budget_pages=-1, price=P)


def test_scenarios_never_assume_l0_and_label_unavailable_ones() -> None:
    rows = backlog_scenarios(6000, price=P, skip_pages=None)
    by = {r.key: r for r in rows}
    assert by["a"].textin_pages == 6000 and by["a"].cny == Decimal("813.6000")
    assert by["a"].available is True
    assert by["b"].available is False and by["b"].cny is None  # no measured saving yet
    rows = backlog_scenarios(6000, price=P, skip_pages=100)
    by = {r.key: r for r in rows}
    assert by["b"].textin_pages == 5900 and by["b"].cny == Decimal("800.0400")
    assert by["b"].available is True
    for k in ("c1", "c2"):
        assert by[k].available is False
        assert "不可用" in by[k].note
    assert by["c1"].textin_pages == 6000 - 2147  # 6000 * 0.3579 = 2147.4 -> 2147
    assert by["c2"].textin_pages == 6000 - 3384  # 6000 * 0.564
    with pytest.raises(ValueError):
        backlog_scenarios(100, price=P, skip_pages=101)
