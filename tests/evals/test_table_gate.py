from __future__ import annotations

import pytest

from ragdemo.evals.table_gate import GATES, GateIncomparable, judge, judge_engines


def _m(n: int, r10: float, mrr: float) -> dict[str, float | int | bool]:
    return {"n": n, "recall_at_10": r10, "recall_at_50": 1.0, "mrr_at_10": mrr, "gated": n >= 10}


BEFORE = {"table/auto": _m(29, 14 / 29, 0.40), "other/auto": _m(71, 63 / 71, 0.70)}


def test_thresholds_are_the_written_ones() -> None:
    assert GATES["table/auto"].max_recall10_drop_cases == 1
    assert GATES["other/auto"].max_recall10_drop_cases == 2
    assert GATES["table/human"].max_recall10_drop_cases == 2
    assert GATES["table/auto"].max_mrr10_drop == 0.03
    assert GATES["other/auto"].max_mrr10_drop == 0.02


def test_one_table_case_drop_passes_two_fail() -> None:
    after_ok = {"table/auto": _m(29, 13 / 29, 0.39), "other/auto": _m(71, 63 / 71, 0.70)}
    assert judge(BEFORE, after_ok, overall_before=0.77, overall_after=0.76).passed
    after_bad = {"table/auto": _m(29, 12 / 29, 0.39), "other/auto": _m(71, 63 / 71, 0.70)}
    res = judge(BEFORE, after_bad, overall_before=0.77, overall_after=0.76)
    assert not res.passed
    assert any("table/auto" in r and "recall@10" in r for r in res.reasons)


def test_mrr_drop_beyond_limit_fails() -> None:
    after = {"table/auto": _m(29, 14 / 29, 0.36), "other/auto": _m(71, 63 / 71, 0.70)}
    res = judge(BEFORE, after, overall_before=0.77, overall_after=0.77)
    assert not res.passed and any("MRR@10" in r for r in res.reasons)


def test_overall_drop_over_two_points_fails() -> None:
    res = judge(BEFORE, BEFORE, overall_before=0.80, overall_after=0.775)
    assert not res.passed and any("总体" in r for r in res.reasons)


def test_changed_case_count_is_incomparable() -> None:
    after = {"table/auto": _m(28, 14 / 28, 0.40), "other/auto": _m(71, 63 / 71, 0.70)}
    with pytest.raises(GateIncomparable):
        judge(BEFORE, after, overall_before=0.77, overall_after=0.77)


def test_small_groups_are_reported_not_gated() -> None:
    before = {**BEFORE, "other/human": _m(3, 1.0, 1.0)}
    after = {**BEFORE, "other/human": _m(3, 0.0, 0.0)}
    res = judge(before, after, overall_before=0.77, overall_after=0.77)
    assert res.passed
    assert any("other/human" in line and "不判门槛" in line for line in res.report)


def test_engine_gate_legacy_baseline_equals_itself_and_new_groups_are_reported() -> None:
    before = {"legacy/auto": _m(100, 0.77, 0.60)}
    after = {
        "legacy/auto": _m(100, 0.77, 0.60),
        "local/human": _m(12, 0.5, 0.3),
    }
    res = judge_engines(before, after, overall_before=0.77, overall_after=0.77)
    assert res.passed and any("local/human" in line for line in res.report)


def test_engine_gate_changed_legacy_n_is_incomparable() -> None:
    with pytest.raises(GateIncomparable):
        judge_engines(
            {"legacy/auto": _m(100, 0.7, 0.6)},
            {"legacy/auto": _m(99, 0.7, 0.6)},
            overall_before=0.7,
            overall_after=0.7,
        )


def test_engine_gate_overall_tolerance_reused() -> None:
    g = {"legacy/auto": _m(100, 0.8, 0.6)}
    res = judge_engines(g, g, overall_before=0.80, overall_after=0.77)
    assert not res.passed and any("总体" in r for r in res.reasons)


def test_engine_gate_e2_local_human_vs_textin_human() -> None:
    base = {"legacy/auto": _m(100, 0.7, 0.6)}
    bad = {**base, "local/human": _m(30, 0.60, 0.3), "textin/human": _m(30, 0.70, 0.3)}
    res = judge_engines(base, bad, overall_before=0.7, overall_after=0.7)
    assert not res.passed and any("E2" in r for r in res.reasons)
    ok = {**base, "local/human": _m(30, 0.66, 0.3), "textin/human": _m(30, 0.70, 0.3)}
    assert judge_engines(base, ok, overall_before=0.7, overall_after=0.7).passed
    thin = {**base, "local/human": _m(29, 0.0, 0.0), "textin/human": _m(30, 0.70, 0.3)}
    res = judge_engines(base, thin, overall_before=0.7, overall_after=0.7)
    assert res.passed and any("证据不足" in line for line in res.report)
