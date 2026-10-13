"""减页规则（纯函数、确定性）：识别"送去解析也拿不到任何内容或数字"的页。

v1 的三条规则在构造上不丢数字：命中页除日期外不得含任何数字（numbers_in 口径，宁可多算）。
整份文档所有页都会被跳过时一页都不跳。规则只给出"命中"，是否执行由调用方按配置
（PARSE_SKIP_RULES，经 T2 门槛与用户批准）决定；未执行的命中记为影子跳页写进台账。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ragdemo.parse.local.layout import PageLayout
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.tiered.types import SKIP_RULE_IDS, SkipRuleId

SKIP_RULES_VERSION: Final = "skip-v1"
DocKind = Literal["announcement", "research_report"]
BLANK_MAX_CHARS: Final = 40
SIGNATURE_MAX_CHARS: Final = 200
DISCLAIMER_TAIL_PAGES: Final = 3
_SIGN = re.compile(r"董事会|监事会|特此公告")
_DISCLAIMER = re.compile(r"免责声明|分析师声明|法律声明|重要声明|特别声明")
_DATE = re.compile(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")


@dataclass(frozen=True)
class PageFacts:
    page: int
    text: str  # 文字层（pdfplumber，剥离装饰之前）
    image_count: int
    curve_count: int


def page_facts(layout: PageLayout) -> PageFacts:
    return PageFacts(
        layout.page,
        layout.text,
        sum(1 for g in layout.graphics if g.kind == "image"),
        sum(1 for g in layout.graphics if g.kind == "curve"),
    )


def non_date_numbers(text: str) -> int:
    return sum(numbers_in(_DATE.sub(" ", text)).values())


def _chars(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


@dataclass(frozen=True)
class SkipDecision:
    page: int
    matched: tuple[SkipRuleId, ...]  # 按 SKIP_RULE_IDS 的顺序


def evaluate_skip_rules(pages: Sequence[PageFacts], *, doc_kind: DocKind) -> list[SkipDecision]:
    n = len(pages)
    out: list[SkipDecision] = []
    for i, p in enumerate(pages):
        chars = _chars(p.text)
        no_numbers = non_date_numbers(p.text) == 0
        hit: set[SkipRuleId] = set()
        if chars < BLANK_MAX_CHARS and p.image_count == 0 and p.curve_count == 0 and no_numbers:
            hit.add("blank_page")
        if (
            doc_kind == "announcement"
            and n >= 2
            and i == n - 1
            and chars < SIGNATURE_MAX_CHARS
            and _SIGN.search(p.text)
            and no_numbers
        ):
            hit.add("signature_tail")
        if (
            doc_kind == "research_report"
            and i >= n - DISCLAIMER_TAIL_PAGES
            and _DISCLAIMER.search(p.text)
            and no_numbers
        ):
            hit.add("research_disclaimer_tail")
        out.append(SkipDecision(p.page, tuple(r for r in SKIP_RULE_IDS if r in hit)))
    return out


def enforced_skips(
    decisions: Sequence[SkipDecision], enforced: frozenset[SkipRuleId]
) -> dict[int, SkipRuleId]:
    chosen: dict[int, SkipRuleId] = {}
    for d in decisions:
        for rule in d.matched:
            if rule in enforced:
                chosen[d.page] = rule
                break
    if decisions and len(chosen) == len(decisions):
        return {}
    return chosen
