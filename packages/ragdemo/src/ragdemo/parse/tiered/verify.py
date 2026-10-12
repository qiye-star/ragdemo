"""分级路由的溯源校验（CLAUDE.md §0：每个数字可溯源）。

- 有文字层的页：引擎输出必须与 PDF 文字层数字双向闭合、关键字双向闭合、文字双向覆盖。
  （L0 另有自己更严的字符闭合与表格原样检查，在 parse/local/structure.py。）
- 无文字层的页：没有原文可对照，用两个独立引擎互相对照替代——数字多重集完全相等、
  关键字多重集相等、表格行互相完全召回。阈值写死在这里并进入引擎指纹。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

from ragdemo.parse.local.compare import plain_text_from_markdown, table_row_recall, text_coverage
from ragdemo.parse.local.number_closure import closure_violations, numbers_in

VERIFY_VERSION: Final = "v1"
TEXT_LAYER_MIN_COVERAGE = 0.97
DUAL_MIN_COVERAGE = 0.90
# 单位、百分号、方向词、中文数字：OCR 认错这些字，数字全对也会把意思改反。
CRITICAL_CHARS: frozenset[str] = frozenset(
    "亿万千百十元%‰增减升降涨跌负亏盈零〇一二三四五六七八九两"
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def _content(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def _critical(text: str) -> Counter[str]:
    return Counter(ch for ch in text if ch in CRITICAL_CHARS)


def critical_char_violations(structured: str, source: str) -> list[str]:
    """structured 中出现次数多于 source 的关键字（已排序）。"""
    src, out = _critical(source), _critical(structured)
    return sorted(ch for ch, c in out.items() if c > src.get(ch, 0))


def verify_against_text_layer(candidate_md: str, page_text: str) -> Verdict:
    plain = plain_text_from_markdown(candidate_md)
    if not _content(plain):
        return Verdict(False, ("引擎输出为空",))
    reasons: list[str] = []
    invented = closure_violations(plain, page_text)
    lost = closure_violations(page_text, plain)
    if invented:
        reasons.append(f"凭空数字：{invented}")
    if lost:
        reasons.append(f"丢失数字：{lost}")
    added = critical_char_violations(plain, page_text)
    missing = critical_char_violations(page_text, plain)
    if added:
        reasons.append(f"关键字新增：{''.join(added)}")
    if missing:
        reasons.append(f"关键字丢失：{''.join(missing)}")
    fwd = text_coverage(_content(page_text), _content(plain))
    back = text_coverage(_content(plain), _content(page_text))
    if fwd < TEXT_LAYER_MIN_COVERAGE:
        reasons.append(f"文字覆盖率 {fwd:.4f} < {TEXT_LAYER_MIN_COVERAGE}")
    if back < TEXT_LAYER_MIN_COVERAGE:
        reasons.append(f"反向覆盖率 {back:.4f} < {TEXT_LAYER_MIN_COVERAGE}（多出了原文没有的文字）")
    return Verdict(not reasons, tuple(reasons))


def verify_dual(primary_md: str, secondary_md: str) -> Verdict:
    a, b = plain_text_from_markdown(primary_md), plain_text_from_markdown(secondary_md)
    if not _content(a) or not _content(b):
        return Verdict(False, ("至少一路输出为空",))
    reasons: list[str] = []
    na, nb = numbers_in(a), numbers_in(b)
    if na != nb:
        only_a, only_b = sorted((na - nb).elements()), sorted((nb - na).elements())
        reasons.append(f"数字不一致：仅主引擎 {only_a}；仅副引擎 {only_b}")
    if _critical(a) != _critical(b):
        reasons.append("关键字（单位/方向/中文数字）不一致")
    ra, rb = table_row_recall(primary_md, secondary_md), table_row_recall(secondary_md, primary_md)
    if ra < 1.0 or rb < 1.0:
        reasons.append(f"表格行不一致：{ra:.4f}/{rb:.4f}")
    agree = min(text_coverage(_content(a), _content(b)), text_coverage(_content(b), _content(a)))
    if agree < DUAL_MIN_COVERAGE:
        reasons.append(f"文字一致度 {agree:.4f} < {DUAL_MIN_COVERAGE}")
    return Verdict(not reasons, tuple(reasons))
