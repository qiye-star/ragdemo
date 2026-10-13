# tests/parse/tiered/test_skip_audit.py
from decimal import Decimal

from ragdemo.parse.tiered.skip_audit import (
    SKIP_GATE_MIN_PAGES,
    BacklogCandidate,
    RuleSavings,
    SkipEvidence,
    judge_rule,
    savings,
)


def _ev(page: int, *, nums: int = 0, refs: int = 0, rule: str = "signature_tail") -> SkipEvidence:
    return SkipEvidence(rule, f"doc{page}", page, nums, refs)  # type: ignore[arg-type]


def test_a_rule_with_enough_clean_validation_pages_is_enforceable() -> None:
    v = judge_rule("signature_tail", [_ev(p) for p in range(1, SKIP_GATE_MIN_PAGES + 1)])
    assert (v.verdict, v.validation_pages, v.reasons) == ("enforceable", 5, ())


def test_too_few_pages_numbers_or_references_make_it_shadow_only() -> None:
    few = judge_rule("signature_tail", [_ev(1), _ev(2)])
    assert few.verdict == "shadow_only" and "证据不足" in few.reasons[0]
    dirty = judge_rule("signature_tail", [_ev(p) for p in range(1, 6)] + [_ev(6, nums=1)])
    assert (dirty.verdict, dirty.pages_with_numbers) == ("shadow_only", 1)
    cited = judge_rule("signature_tail", [_ev(p) for p in range(1, 6)] + [_ev(6, refs=2)])
    assert (cited.verdict, cited.referenced_blocks) == ("shadow_only", 2)


def test_other_rules_evidence_is_ignored() -> None:
    v = judge_rule("blank_page", [_ev(p) for p in range(1, 9)])
    assert v.validation_pages == 0 and v.verdict == "shadow_only"


def test_savings_exclude_pages_that_l0_takes_for_free() -> None:
    c = [
        BacklogCandidate("blank_page", "a", 1, l0_accepted=False),
        BacklogCandidate("blank_page", "a", 2, l0_accepted=False),
        BacklogCandidate("signature_tail", "a", 9, l0_accepted=True),
    ]
    s = savings(c, price=Decimal("0.1356"))
    assert s["blank_page"] == RuleSavings(2, 2, Decimal("0.2712"))
    assert s["signature_tail"] == RuleSavings(1, 0, Decimal("0"))
