"""分级路由的溯源校验（CLAUDE.md §0：每个数字可溯源）。

- 有文字层的页：引擎输出必须与 PDF 文字层数字双向闭合、关键字双向闭合、文字双向覆盖，
  并且「数字 + 关键字」的阅读顺序序列完全相等（防止数字/单位/表头/负号换位）。
  （L0 另有自己更严的字符闭合与表格原样检查，在 parse/local/structure.py。）
- 无文字层的页：没有原文可对照，用两个独立引擎互相对照替代——数字多重集与顺序序列完全相等、
  关键字多重集相等、表格逐行逐格（含表头）按顺序完全相等。
- 被校验的是去标记后的纯文本，但入库的是原 markdown：因此任何隐藏内容的标记
  （注释、未知标签、多余属性、数字字符实体）一律拒绝。

阈值写死在这里并进入引擎指纹。零容忍：宁可误报把页推到 L3，不可放过。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Final

from ragdemo.parse.local.compare import plain_text_from_markdown, text_coverage
from ragdemo.parse.local.number_closure import (
    closure_violations,
    fold_text,
    number_spans,
    numbers_in,
)

VERIFY_VERSION: Final = "v2"
TEXT_LAYER_MIN_COVERAGE = 0.97
DUAL_MIN_COVERAGE = 0.90
# 单位、百分号、方向词、中文数字、否定词、口径词、币种：OCR/引擎认错这些字，
# 数字全对也会把意思改反。
CRITICAL_CHARS: frozenset[str] = frozenset(
    "亿万千百十元%‰增减升降涨跌负亏盈零〇一二三四五六七八九两"
    "不未无非否"  # 否定
    "同环末初"  # 同比/环比、期末/期初
    "美港欧日"  # 币种
)

_ALLOWED_TAG = re.compile(
    r"<\s*/?\s*(?:table|thead|tbody|tr|td|th|br)"
    r"(?:\s+(?:rowspan|colspan)\s*=\s*(?:\"\d+\"|'\d+'|\d+))*\s*/?\s*>",
    re.IGNORECASE,
)
_ANY_ANGLE = re.compile(r"<[^>]*>")
_NUMERIC_ENTITY = re.compile(r"&#(?:\d+|[xX][0-9a-fA-F]+);")
_TABLE = re.compile(r"(<table\b.*?</table>)", re.DOTALL | re.IGNORECASE)
_TR_SPLIT = re.compile(r"<tr\b[^>]*>", re.IGNORECASE)
_CELL_SPLIT = re.compile(r"<t[dh]\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def _content(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def _critical(text: str) -> Counter[str]:
    return Counter(ch for ch in fold_text(text) if ch in CRITICAL_CHARS)


def critical_char_violations(structured: str, source: str) -> list[str]:
    """structured 中出现次数多于 source 的关键字（已排序）。"""
    src, out = _critical(source), _critical(structured)
    return sorted(ch for ch, c in out.items() if c > src.get(ch, 0))


def critical_stream(text: str) -> list[str]:
    """阅读顺序下的「数字 token 与关键字」交错序列。两份文本必须逐项相等。"""
    folded = fold_text(text)
    out: list[str] = []
    pos = 0
    for start, end, token in number_spans(text):
        out.extend(ch for ch in folded[pos:start] if ch in CRITICAL_CHARS)
        out.append(token)
        pos = end
    out.extend(ch for ch in folded[pos:] if ch in CRITICAL_CHARS)
    return out


def markup_violations(md: str) -> list[str]:
    """入库的是 md，校验的是去标记后的文本；凡能让两者不一致的标记都在此拒绝。"""
    found: list[str] = []
    if "<!--" in md:
        found.append("HTML 注释")
    for span in _ANY_ANGLE.findall(md):
        if span.startswith("<!--"):
            continue
        if not _ALLOWED_TAG.fullmatch(span):
            found.append(f"不允许的标签或属性 {span[:40]!r}")
    if _NUMERIC_ENTITY.search(md):
        found.append("数字字符实体")
    return found


def _stream_mismatch(a: str, b: str) -> bool:
    return critical_stream(a) != critical_stream(b)


def verify_against_text_layer(candidate_md: str, page_text: str) -> Verdict:
    bad = markup_violations(candidate_md)
    if bad:
        return Verdict(False, (f"输出含不允许的标记：{'；'.join(bad)}",))
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
    if _stream_mismatch(plain, page_text):
        reasons.append("数字/关键字顺序与文字层不一致（换位、换行、换列、单位或符号错位）")
    return Verdict(not reasons, tuple(reasons))


def _cell(raw: str) -> str:
    return re.sub(r"\s+", "", fold_text(plain_text_from_markdown(raw)))


def _html_rows(table: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for tr in _TR_SPLIT.split(table)[1:]:
        cells = tuple(_cell(c) for c in _CELL_SPLIT.split(tr)[1:])
        if any(cells):
            rows.append(cells)
    return rows


def _pipe_rows(segment: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for line in segment.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if set(re.sub(r"\s+", "", s.replace("|", ""))) <= {"-", ":"}:
            continue  # 分隔行（以及空行 ||）
        s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        cells = tuple(_cell(c) for c in s.split("|"))
        if any(cells):
            rows.append(cells)
    return rows


def table_cell_rows(md: str) -> list[tuple[str, ...]]:
    """文档顺序下所有表格行（含表头）的归一化单元格元组；HTML 与竖线表统一处理。"""
    rows: list[tuple[str, ...]] = []
    for i, seg in enumerate(_TABLE.split(md)):
        rows.extend(_html_rows(seg) if i % 2 else _pipe_rows(seg))
    return rows


def verify_dual(primary_md: str, secondary_md: str) -> Verdict:
    """两个独立引擎互相对照，用于无文字层的页。

    已知局限（无法消除）：两个引擎犯相同的错误（相同的误识别、相同的漏行）时，
    互相对照无法发现——这里没有原文可比。该风险只能靠引擎独立性与抽样评测控制。

    要求：数字多重集相等；关键字多重集相等；数字+关键字阅读顺序序列相等；
    表格按行、按单元格、含表头完全相等；文字一致度 >= DUAL_MIN_COVERAGE；
    两路输出都不含隐藏内容的标记。"""
    bad = [
        f"{name}输出含不允许的标记：{'；'.join(v)}"
        for name, md in (("主引擎", primary_md), ("副引擎", secondary_md))
        if (v := markup_violations(md))
    ]
    if bad:
        return Verdict(False, tuple(bad))
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
    ra, rb = table_cell_rows(primary_md), table_cell_rows(secondary_md)
    if ra != rb:
        first = next(
            (i for i, (x, y) in enumerate(zip(ra, rb, strict=False)) if x != y),
            min(len(ra), len(rb)),
        )
        reasons.append(
            f"表格行不一致：主 {len(ra)} 行 / 副 {len(rb)} 行，首个差异在第 {first + 1} 行"
        )
    if _stream_mismatch(a, b):
        reasons.append("数字/关键字顺序不一致（换位、换行、换列、单位或符号错位）")
    agree = min(text_coverage(_content(a), _content(b)), text_coverage(_content(b), _content(a)))
    if agree < DUAL_MIN_COVERAGE:
        reasons.append(f"文字一致度 {agree:.4f} < {DUAL_MIN_COVERAGE}")
    return Verdict(not reasons, tuple(reasons))
