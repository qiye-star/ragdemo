"""积压 / 增量成本估算与启用前后每页成本（纯函数，Decimal，不让模型产出任何数值）。

现实口径（2026-10-13 裁决）：生产只有 L3（TextIn）；L0 关闭、L1 不可用、L2 未建，跳页规则很小。
所以积压场景的主线是"全送 TextIn"与"扣除已实测的跳页"；L0 场景只为完整性保留，必须标注不可用，
任何节省都不得算进可用场景。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from ragdemo.parse.tiered.ledger import TierRow

_Q4: Final = Decimal("0.0001")


@dataclass(frozen=True)
class GroupEstimate:
    name: str
    pages: int
    l0_share: Decimal  # 按页路由时 L0 接手的页占比；整份 L3 的组为 0
    skipped_pages: int = 0

    def __post_init__(self) -> None:
        if self.pages < 0 or not Decimal(0) <= self.l0_share <= Decimal(1):
            raise ValueError(f"{self.name}：页数 {self.pages} 或 L0 份额 {self.l0_share} 非法")
        if self.skipped_pages < 0 or self.skipped_pages > self.pages - l0_pages(self):
            raise ValueError(f"{self.name}：跳页 {self.skipped_pages} 超过非 L0 页")


def l0_pages(g: GroupEstimate) -> int:
    return int((Decimal(g.pages) * g.l0_share).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CostEstimate:
    pages: int
    textin_pages: int
    cny: Decimal
    per_page: Decimal


def estimate(groups: Sequence[GroupEstimate], *, price: Decimal) -> CostEstimate:
    pages = sum(g.pages for g in groups)
    textin = sum(g.pages - l0_pages(g) - g.skipped_pages for g in groups)
    cny = price * textin
    per_page = (cny / pages).quantize(_Q4, rounding=ROUND_HALF_UP) if pages else Decimal(0)
    return CostEstimate(pages, textin, cny, per_page)


@dataclass(frozen=True)
class CostComparison:
    pages: int
    billed_pages: int
    unbilled_l3_pages: int
    actual_cny: Decimal
    counterfactual_cny: Decimal
    actual_per_page: Decimal
    saving_share: Decimal


def compare_costs(rows: Sequence[TierRow], *, price: Decimal) -> CostComparison:
    pages = sum(r.pages for r in rows)
    if pages == 0:
        raise ValueError("台账里没有页，无从比较")
    l3 = next((r for r in rows if r.tier == "L3"), None)
    billed = l3.billed_pages if l3 is not None else 0
    unbilled = (l3.pages - l3.billed_pages) if l3 is not None else 0
    actual = (sum((r.cost_cny for r in rows), Decimal(0)) + price * unbilled).quantize(_Q4)
    counterfactual = (price * pages).quantize(_Q4)
    return CostComparison(
        pages=pages,
        billed_pages=billed,
        unbilled_l3_pages=unbilled,
        actual_cny=actual,
        counterfactual_cny=counterfactual,
        actual_per_page=(actual / pages).quantize(_Q4, rounding=ROUND_HALF_UP),
        saving_share=(1 - actual / counterfactual).quantize(_Q4, rounding=ROUND_HALF_UP),
    )


def unverified_rate(unverified: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(unverified) / Decimal(total)).quantize(_Q4, rounding=ROUND_HALF_UP)


# --- 积压场景（只读规划；全部送 TextIn 是唯一可用的基线）-------------------------------

# L0 假设份额：计划表 / 留出集的既有口径（仅用于"不可用"场景的完整性展示）。
L0_SHARE_CONSERVATIVE: Final = Decimal("0.3579")
L0_SHARE_OPTIMISTIC: Final = Decimal("0.564")
L0_UNAVAILABLE_NOTE: Final = (
    "不可用：L0 关闭（留出集 G3 未过），须有经验证的 L0 v3 才成立；不计入任何可用预算"
)


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    available: bool
    textin_pages: int | None
    cny: Decimal | None
    note: str


def backlog_scenarios(pages: int, *, price: Decimal, skip_pages: int | None) -> list[Scenario]:
    if pages < 0 or (skip_pages is not None and not 0 <= skip_pages <= pages):
        raise ValueError(f"页数 {pages} 或跳页 {skip_pages} 非法")
    out = [
        Scenario("a", "全部送 TextIn", True, pages, (price * pages).quantize(_Q4), "现行生产现实"),
    ]
    if skip_pages is None:
        out.append(
            Scenario(
                "b",
                "扣除跳页规则的实测节省",
                False,
                None,
                None,
                "待 T2 实测，未实测前不假设任何节省",
            )
        )
    else:
        n = pages - skip_pages
        out.append(
            Scenario(
                "b",
                "扣除跳页规则的实测节省",
                True,
                n,
                (price * n).quantize(_Q4),
                f"跳 {skip_pages} 页",
            )
        )
    for key, share, label in (
        ("c1", L0_SHARE_CONSERVATIVE, "L0 保守"),
        ("c2", L0_SHARE_OPTIMISTIC, "L0 乐观"),
    ):
        e = estimate([GroupEstimate("积压", pages, share)], price=price)
        out.append(
            Scenario(
                key,
                f"{label}（L0 份额 {share}）",
                False,
                e.textin_pages,
                e.cny.quantize(_Q4),
                L0_UNAVAILABLE_NOTE,
            )
        )
    return out


# --- 页预算规划 -------------------------------------------------------------------------

DEFAULT_PRIORITY: Final = ("first", "none", "second")


@dataclass(frozen=True)
class BacklogDoc:
    doc_id: int
    tier: str  # ParseTier.value：first / second / none
    pages: int
    publish_at: datetime
    title: str
    pages_estimated: bool = False  # 读不到 PDF，页数是经验估算


@dataclass(frozen=True)
class PlanLine:
    doc: BacklogDoc
    cum_pages: int
    cum_cny: Decimal


@dataclass(frozen=True)
class BudgetPlan:
    budget_pages: int
    selected: tuple[PlanLine, ...]
    remaining: tuple[BacklogDoc, ...]
    used_pages: int
    used_cny: Decimal
    budget_left_pages: int
    remaining_pages: int
    remaining_cny: Decimal


def order_backlog(docs: Sequence[BacklogDoc], priority: Sequence[str]) -> list[BacklogDoc]:
    """档位按 priority 顺序；档内按公告日新到旧，同日按 doc_id 升序（确定性）。"""
    rank = {t: i for i, t in enumerate(priority)}
    unknown = {d.tier for d in docs} - rank.keys()
    if unknown:
        raise ValueError(f"优先级里缺档位：{sorted(unknown)}")
    return sorted(docs, key=lambda d: (rank[d.tier], -d.publish_at.timestamp(), d.doc_id))


def plan_budget(
    docs: Sequence[BacklogDoc],
    *,
    budget_pages: int,
    price: Decimal,
    priority: Sequence[str] = DEFAULT_PRIORITY,
    strict_order: bool = False,
) -> BudgetPlan:
    """按优先级顺序装入整份文档（不拆文档）。默认越过放不下的大文档继续装更靠后的小文档；
    strict_order=True 时遇到第一份放不下的就停（严格按优先级，不跳过）。"""
    if budget_pages < 0:
        raise ValueError("页预算不得为负")
    selected: list[PlanLine] = []
    remaining: list[BacklogDoc] = []
    used = 0
    blocked = False
    for d in order_backlog(docs, priority):
        if not blocked and used + d.pages <= budget_pages:
            used += d.pages
            selected.append(PlanLine(d, used, (price * used).quantize(_Q4)))
        else:
            remaining.append(d)
            blocked = blocked or strict_order
    rem_pages = sum(d.pages for d in remaining)
    return BudgetPlan(
        budget_pages=budget_pages,
        selected=tuple(selected),
        remaining=tuple(remaining),
        used_pages=used,
        used_cny=(price * used).quantize(_Q4),
        budget_left_pages=budget_pages - used,
        remaining_pages=rem_pages,
        remaining_cny=(price * rem_pages).quantize(_Q4),
    )
