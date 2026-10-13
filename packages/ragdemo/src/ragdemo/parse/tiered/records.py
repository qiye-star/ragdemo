# packages/ragdemo/src/ragdemo/parse/tiered/records.py
"""逐页台账的数据类（纯，无 I/O）。parse/textin.py 的 ParseResult 引用 ParseLedger，
所以本模块除 tiered.types 外不得 import parse 包内的任何模块。校验与迁移 034 的 CHECK 一一对应。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ragdemo.parse.tiered.types import SkipRuleId

# "L0" 是休眠的未来值：L0 v2 未启用（G9 未过），验证通过前不会写入 L0 行；
# 当前只用 "L3" 与 "skipped"。
LedgerTier = Literal["L0", "L3", "skipped"]
LEDGER_TIERS: Final[tuple[LedgerTier, ...]] = ("L0", "L3", "skipped")
TierVerdictValue = Literal["passed", "failed", "not_run"]
P3StateValue = Literal["pass", "fail", "unverifiable_scan"]
DocStateValue = Literal[
    "accepted",
    "accepted_with_scan",
    "partially_accepted",
    "textin_unverified",
    "rejected",
    "empty_document",
]


@dataclass(frozen=True)
class PageRecord:
    page: int
    tier: LedgerTier
    engine: str
    page_kind: str
    reasons: tuple[str, ...] = ()
    skip_rule: SkipRuleId | None = None
    shadow_skip_rules: tuple[SkipRuleId, ...] = ()
    textin_billed: bool = False
    textin_json_ref: str | None = None
    tier_verdict: TierVerdictValue = "not_run"
    p3_state: P3StateValue | None = None
    unverified_numbers: int = 0
    output_numbers: int = 0
    image_ocr_numbers: int = 0
    coverage_ratio: float | None = None
    table_blocks: int = 0
    figure_count: int = 0
    chart_regions: int = 0
    checks: tuple[tuple[str, bool, str, str], ...] = ()  # (检查号, 通过, 状态, 说明)

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.page < 1:
            problems.append(f"页码 {self.page}")
        if self.tier not in LEDGER_TIERS:
            problems.append(f"层级 {self.tier!r}")
        if not self.engine or not self.page_kind:
            problems.append("engine / page_kind 为空")
        if (self.tier == "skipped") != (self.skip_rule is not None):
            problems.append("skip_rule 必须且只能出现在跳页上")
        if self.textin_billed and self.tier != "L3":
            problems.append("只有 L3 页可以计费")
        if self.textin_json_ref is not None and self.tier != "L3":
            problems.append("只有 L3 页有 TextIn JSON")
        if (self.tier == "skipped") != (self.p3_state is None):
            problems.append("跳页没有 P3，其余页必须有")
        counts = (
            self.unverified_numbers,
            self.output_numbers,
            self.image_ocr_numbers,
            self.table_blocks,
            self.figure_count,
            self.chart_regions,
        )
        if any(c < 0 for c in counts):
            problems.append("计数为负")
        if self.unverified_numbers > self.output_numbers:
            problems.append("未溯源数字多于产出数字")
        if problems:
            raise ValueError(f"PageRecord p{self.page} 不一致：{'；'.join(problems)}")


@dataclass(frozen=True)
class DocRecord:
    state: DocStateValue
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    unverified_numbers: int
    scan_pages: int
    page_count: int
    l0_pages: int
    l3_pages: int
    skipped_pages: int
    billed_pages: int
    fallback_reason: str | None
    acceptance_version: str

    def __post_init__(self) -> None:
        counts = (
            self.unverified_numbers,
            self.scan_pages,
            self.page_count,
            self.l0_pages,
            self.l3_pages,
            self.skipped_pages,
            self.billed_pages,
        )
        if any(c < 0 for c in counts):
            raise ValueError("DocRecord 计数为负")
        if self.l0_pages + self.l3_pages + self.skipped_pages != self.page_count:
            raise ValueError(
                f"页数不守恒：L0 {self.l0_pages} + L3 {self.l3_pages} + 跳页 {self.skipped_pages}"
                f" ≠ {self.page_count}"
            )


@dataclass(frozen=True)
class ParseLedger:
    routes_ref: str
    pages: tuple[PageRecord, ...]
    doc: DocRecord

    def __post_init__(self) -> None:
        nums = [p.page for p in self.pages]
        if nums != sorted(set(nums)):
            raise ValueError(f"台账页码必须严格递增：{nums[:20]}")
        by_tier = {t: sum(1 for p in self.pages if p.tier == t) for t in LEDGER_TIERS}
        if (by_tier["L0"], by_tier["L3"], by_tier["skipped"]) != (
            self.doc.l0_pages,
            self.doc.l3_pages,
            self.doc.skipped_pages,
        ):
            raise ValueError(f"逐页层级计数 {by_tier} 与文档结论不一致")
