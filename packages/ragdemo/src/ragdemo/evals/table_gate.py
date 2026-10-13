"""表格专项评测门槛（阈值写死在计划"评测口径"一节，改动须新开计划修订）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from ragdemo.evals.grouping import MIN_GROUP_SIZE

_EPS: Final = 1e-9
OVERALL_MAX_RECALL10_DROP: Final = 0.02


@dataclass(frozen=True)
class GroupGate:
    max_recall10_drop_cases: int
    max_mrr10_drop: float


GATES: Final[Mapping[str, GroupGate]] = MappingProxyType(
    {
        "table/auto": GroupGate(max_recall10_drop_cases=1, max_mrr10_drop=0.03),
        "other/auto": GroupGate(max_recall10_drop_cases=2, max_mrr10_drop=0.02),
        "table/human": GroupGate(max_recall10_drop_cases=2, max_mrr10_drop=0.03),
        "other/human": GroupGate(max_recall10_drop_cases=2, max_mrr10_drop=0.02),
    }
)


class GateIncomparable(ValueError):
    """前后两次运行的同一分组题数不同：评测集或语料变了，指标不可比。"""


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    report: tuple[str, ...]


def judge(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    *,
    overall_before: float,
    overall_after: float,
) -> GateResult:
    reasons: list[str] = []
    report: list[str] = []
    for group in sorted(set(before) | set(after)):
        if group not in before or group not in after:
            raise GateIncomparable(f"{group} 只出现在一次运行里")
        b, a = before[group], after[group]
        n = int(b["n"])
        if int(a["n"]) != n:
            raise GateIncomparable(f"{group} 题数 {n} → {int(a['n'])}")
        d10 = float(b["recall_at_10"]) - float(a["recall_at_10"])
        dmrr = float(b["mrr_at_10"]) - float(a["mrr_at_10"])
        line = (
            f"{group} n={n} recall@10 {float(b['recall_at_10']):.4f}→{float(a['recall_at_10']):.4f}"
            f" MRR@10 {float(b['mrr_at_10']):.4f}→{float(a['mrr_at_10']):.4f}"
        )
        if n < MIN_GROUP_SIZE or group not in GATES:
            report.append(f"{line}（n<{MIN_GROUP_SIZE} 或无门槛，不判门槛）")
            continue
        gate = GATES[group]
        report.append(line)
        if d10 > gate.max_recall10_drop_cases / n + _EPS:
            reasons.append(
                f"{group} recall@10 下降 {d10:.4f} > {gate.max_recall10_drop_cases} 题"
                f"（{gate.max_recall10_drop_cases / n:.4f}）"
            )
        if dmrr > gate.max_mrr10_drop + _EPS:
            reasons.append(f"{group} MRR@10 下降 {dmrr:.4f} > {gate.max_mrr10_drop}")
    overall_drop = overall_before - overall_after
    if overall_drop > OVERALL_MAX_RECALL10_DROP + _EPS:
        reasons.append(f"总体 recall@10 下降 {overall_drop:.4f} > {OVERALL_MAX_RECALL10_DROP}")
    return GateResult(not reasons, tuple(reasons), tuple(report))


E2_MIN_N: Final = 30
E2_MAX_GAP: Final = 0.05


def judge_engines(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    *,
    overall_before: float,
    overall_after: float,
) -> GateResult:
    """按解析来源分组的前后比较（L0 + TextIn 分页接入计划 T11，不新造阈值）。

    - 两次都有的分组：题数必须相同（否则不可比），只报告变化；
    - 只在一次出现的分组（接入后新出现的 local/* 等）：只报告；
    - 总体 recall@10 沿用 OVERALL_MAX_RECALL10_DROP；
    - E2：local/human 与 textin/human 各 >= 30 题时 local recall@10 >= textin - 0.05；
      不足记"证据不足"。
    """
    reasons: list[str] = []
    report: list[str] = []
    for group in sorted(set(before) | set(after)):
        if group not in before or group not in after:
            side = after if group in after else before
            when = "接入后" if group in after else "接入前"
            report.append(
                f"{group} n={int(side[group]['n'])} 仅出现在{when}一次运行里（只报告，不判门槛）"
            )
            continue
        b, a = before[group], after[group]
        if int(b["n"]) != int(a["n"]):
            raise GateIncomparable(f"{group} 题数 {int(b['n'])} → {int(a['n'])}")
        report.append(
            f"{group} n={int(b['n'])} recall@10 {float(b['recall_at_10']):.4f}→"
            f"{float(a['recall_at_10']):.4f} MRR@10 {float(b['mrr_at_10']):.4f}→"
            f"{float(a['mrr_at_10']):.4f}"
        )
    loc, tin = after.get("local/human"), after.get("textin/human")
    if loc is None or tin is None or min(int(loc["n"]), int(tin["n"])) < E2_MIN_N:
        report.append(f"E2 证据不足（local/human 与 textin/human 各需 >= {E2_MIN_N} 题）")
    else:
        gap = float(tin["recall_at_10"]) - float(loc["recall_at_10"])
        report.append(
            f"E2 local/human {float(loc['recall_at_10']):.4f}"
            f" vs textin/human {float(tin['recall_at_10']):.4f}"
        )
        if gap > E2_MAX_GAP + _EPS:
            reasons.append(f"E2 local/human recall@10 比 textin/human 低 {gap:.4f} > {E2_MAX_GAP}")
    overall_drop = overall_before - overall_after
    if overall_drop > OVERALL_MAX_RECALL10_DROP + _EPS:
        reasons.append(f"总体 recall@10 下降 {overall_drop:.4f} > {OVERALL_MAX_RECALL10_DROP}")
    return GateResult(not reasons, tuple(reasons), tuple(report))
