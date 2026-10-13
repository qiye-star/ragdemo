# packages/ragdemo/src/ragdemo/parse/tiered/skip_audit.py
"""减页规则的门槛判定与节省估算（纯函数；I/O 在 scripts/skip_audit.py）。门槛在
docs/10.12-plan/验收记录/减页规则测量.md 事先登记，代码常量与之一致，不得事后调整。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from ragdemo.parse.tiered.types import SKIP_RULE_IDS, SkipRuleId

SKIP_GATE_MIN_PAGES: Final = 5


@dataclass(frozen=True)
class SkipEvidence:
    rule: SkipRuleId
    doc_key: str
    page: int
    block_numbers: int  # 该页 TextIn 叶子块里的非日期数字个数
    referenced_blocks: int  # 该页块被事实表 / 评测引用的个数


@dataclass(frozen=True)
class RuleVerdict:
    rule: SkipRuleId
    validation_pages: int
    pages_with_numbers: int
    referenced_blocks: int
    verdict: Literal["enforceable", "shadow_only"]
    reasons: tuple[str, ...]


def judge_rule(rule: SkipRuleId, evidence: Sequence[SkipEvidence]) -> RuleVerdict:
    ev = [e for e in evidence if e.rule == rule]
    reasons: list[str] = []
    if len(ev) < SKIP_GATE_MIN_PAGES:
        reasons.append(f"验证页 {len(ev)} < {SKIP_GATE_MIN_PAGES}，证据不足")
    with_numbers = sum(1 for e in ev if e.block_numbers > 0)
    if with_numbers:
        reasons.append(f"{with_numbers} 个验证页的 TextIn 块含非日期数字")
    refs = sum(e.referenced_blocks for e in ev)
    if refs:
        reasons.append(f"{refs} 个验证页的块被事实表或评测引用")
    return RuleVerdict(
        rule,
        len(ev),
        with_numbers,
        refs,
        "shadow_only" if reasons else "enforceable",
        tuple(reasons),
    )


@dataclass(frozen=True)
class BacklogCandidate:
    rule: SkipRuleId  # 该页命中的第一条规则（与 enforced_skips 同序）
    doc_key: str
    page: int
    l0_accepted: bool  # L0 开启时这一页本来就不花钱


@dataclass(frozen=True)
class RuleSavings:
    candidate_pages: int
    incremental_pages: int
    incremental_cny: Decimal


def savings(
    candidates: Sequence[BacklogCandidate], *, price: Decimal
) -> dict[SkipRuleId, RuleSavings]:
    out: dict[SkipRuleId, RuleSavings] = {}
    for rule in SKIP_RULE_IDS:
        mine = [c for c in candidates if c.rule == rule]
        if not mine:
            continue
        inc = sum(1 for c in mine if not c.l0_accepted)
        out[rule] = RuleSavings(len(mine), inc, price * inc)
    return out


INK_GRAY_THRESHOLD: Final = 180  # 灰度低于此值的像素算墨迹（事先登记于减页规则测量.md）
INK_RATIO_REVIEW: Final = 0.005  # 墨迹比超过它的 blank_page 命中页逐页目检


def ink_ratio(gray: bytes) -> float:
    """8 位灰度像素里"墨迹"（灰度 < INK_GRAY_THRESHOLD）所占比例；空输入为 0。"""
    if not gray:
        return 0.0
    return sum(1 for v in gray if v < INK_GRAY_THRESHOLD) / len(gray)


def drawing_group(kinds: Iterable[str]) -> str:
    """页面绘制对象的分组标签：含 image / curve 的优先（blank_page 已排除这两类），
    其次只有 rect / line，否则 none。"""
    present = set(kinds)
    if "image" in present:
        return "image"
    if "curve" in present:
        return "curve"
    if present & {"rect", "line"}:
        return "rect_line"
    return "none"


def summary_base(title: str) -> str | None:
    """标题含"摘要"时返回去掉它的标题（用于找同名全文）；否则 None。"""
    if "摘要" not in title:
        return None
    return title.replace("摘要", "").strip()
