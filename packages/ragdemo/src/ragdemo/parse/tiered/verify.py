"""分级路由的溯源校验（CLAUDE.md §0：每个数字可溯源）。

- 有文字层的页：引擎输出必须与 PDF 文字层数字双向闭合、关键字双向闭合、文字双向覆盖，
  并且「数字 + 关键字」的阅读顺序序列完全相等（防止数字/单位/表头/负号换位）。
  （L0 另有自己更严的字符闭合与表格原样检查，在 parse/local/structure.py。）
- 无文字层的页：没有原文可对照，用两个独立引擎互相对照替代——数字多重集与顺序序列完全相等、
  关键字多重集相等、表格逐行逐格（含表头）按顺序完全相等。
- 被校验的是去标记后的纯文本，但入库的是原 markdown：因此任何隐藏内容的标记
  （注释、未知标签、多余属性、数字字符实体）一律拒绝。

阈值写死在这里并进入引擎指纹。零容忍：宁可误报把页推到 L3，不可放过。

文字层路径还要求：内容字符（字母/数字/汉字，全角折叠后）的有序序列与文字层完全相等
（防止标签/表头/主体名称换位），并拒绝含空单元格、rowspan/colspan（值 1 视为无）、
行单元格数不一致的表格（这些会让数字在相同文字顺序下换列）。

已知局限（续 3）：GFM 脚注标签自动编号 `[^7]`、代码围栏 info string 里藏数字、
删除线/图片 alt/行内链接（见上）；U+2028/U+2029/U+0085 等非 LF 的行分隔符未归一化。
空值占位符按「独立且不贴字母数字」识别，贴着括号/汉字的 `-` 不计入（两侧一致时无影响）。

已知局限（续 2）：分隔行（定界行）单元格数与表头不一致时仍按表处理（更严）；`# a | b` 形式的标题行、
缩进或围栏代码块里的竖线按表处理（更严）；U+2500/U+2027/U+1680 等非破折号字符不折叠为减号；
`</ td>` 的浏览器注释怪癖；引用块/列表里的表只是偶然被拒（`>` 是关键字）。

已知局限（续）：markdown 渲染与被校验文本的其他分歧——删除线 ~~…~~、图片 alt 文本、
行内链接、脚注 * 在行间移动、组合叠加字符；覆盖率检查用 _content 而不做 NFKC，
全角数字/罗马数字会产生误报。

已知局限（有意不在此修复，T7 须度量误报率并据此决策）：
- 两个引擎犯相同错误时，verify_dual 无法发现（没有原文可比）。
- 渲染结果与被校验文本可能不同的 markdown 语义：有序列表自动重编号、引用式链接定义、
  未闭合的 <span title= 之类；本模块只拒绝能识别的隐藏标记。
- 财务报表页识别（classify.py）可能漏判：无标题、行标记不足 2 个、数字不足 20 个的续页。
- 误报来源：对 border="1" 等属性的严格拒绝（若很常见，T7 将提议剥离一个无害属性白名单）；
  文字层按列输出而引擎按行输出时顺序校验必然误报。
  双引擎之间文字排序方式不同（如分栏阅读顺序）同样会被内容顺序校验误报。"""

from __future__ import annotations

import html
import re
import unicodedata
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

VERIFY_VERSION: Final = "v8"
TEXT_LAYER_MIN_COVERAGE = 0.97
DUAL_MIN_COVERAGE = 0.90
# 单位、百分号、方向词、中文数字、否定词、口径词、币种：OCR/引擎认错这些字，
# 数字全对也会把意思改反。
CRITICAL_CHARS: frozenset[str] = frozenset(
    "亿万千百十元%‰增减升降涨跌负亏盈零〇一二三四五六七八九两"
    "不未无非否"  # 否定
    "同环末初"  # 同比/环比、期末/期初
    "美港欧日"  # 币种
    "≥≤<>▲▼△▽↑↓±"  # 比较与涨跌符号
)

# HTML 空白只有 空格/制表/换行/回车/换页（Python 的 \s 还含 NBSP、U+3000 等，HTML 不当空白）；
# `<` 或 `</` 之后不得有任何空白：那不是标签，渲染器把它当字面文字，其中的数字是可见的。
_H = r"[ \t\n\r\f]"
_ALLOWED_TAG = re.compile(
    r"</?(?:table|thead|tbody|tr|td|th|br)"
    rf"(?:{_H}+(?:rowspan|colspan){_H}*={_H}*(?:\"[0-9]+\"|'[0-9]+'|[0-9]+))*{_H}*/?>",
    re.IGNORECASE | re.ASCII,
)
_ANY_ANGLE = re.compile(r"<[^>]*>")
# 与 html.unescape 同一套实体切分（含不带分号的写法）；只放行字面转义用的几个。
_ENTITY = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
_ENTITY_OK: Final = frozenset({"&amp;", "&lt;", "&gt;", "&quot;", "&nbsp;"})
_ATTR_NAME = re.compile(rf"\b(rowspan|colspan){_H}*=", re.IGNORECASE | re.ASCII)
_TABLE_TAG = re.compile(r"<(/?)(table|thead|tbody|tr|td|th)\b([^>]*)>", re.IGNORECASE | re.ASCII)
_BR = re.compile(r"<br\b[^>]*>", re.IGNORECASE | re.ASCII)
_BLANK_LINE = re.compile(r"\n[ \t\r]*\n")
_SPAN = re.compile(rf"(rowspan|colspan){_H}*={_H}*[\"']?([0-9]+)", re.IGNORECASE | re.ASCII)
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


_RUN = re.compile(r"[-/*…]+|\.{2,}")


def _nl(text: str) -> str:
    """CRLF / 单独 CR 统一为 LF：渲染器按这些换行断行，门禁必须按同样方式看。"""
    return re.sub(r"\r\n?", "\n", text)


def _is_separator_row(line: str) -> bool:
    chars = set(re.sub(r"\s+", "", line.replace("|", "")))
    return "|" in line and bool(chars) and chars <= {"-", ":"}


def critical_stream(text: str) -> list[str]:
    """阅读顺序下的「数字 token、关键字、空值占位符」交错序列。两份文本必须逐项相等。

    占位符：独立（两侧不贴字母/数字）的 -/—/–//、*、…、.. 连续串，如财报里表示「无」的 `-`。
    它出现的位置与个数必须与文字层一致，否则数字会在列/年份之间悄悄换位。
    不计入：竖线表分隔行、行首列表标记、整行 3 个以上的分隔线、贴着数字的符号。"""
    folded = "\n".join("" if _is_separator_row(ln) else ln for ln in fold_text(text).split("\n"))
    spans = number_spans(folded)
    events: list[tuple[int, str]] = [(a, tok) for a, _, tok in spans]
    inside = {i for a, e, _ in spans for i in range(a, e)}
    starts = sorted(a for a, _, _ in spans)
    for i, ch in enumerate(folded):
        if ch in CRITICAL_CHARS and i not in inside:
            events.append((i, ch))
    for m in _RUN.finditer(folded):
        start, end = m.span()
        if start in inside:
            continue
        cut = False
        for ns in starts:  # 紧贴的负号属于数字 token
            if start <= ns < end:
                end, cut = ns, True
                break
        if end <= start:
            continue
        if start > 0 and folded[start - 1].isalnum():
            continue
        if not cut and end < len(folded) and folded[end].isalnum():
            continue
        line_start = folded.rfind("\n", 0, start) + 1
        line_end = folded.find("\n", end)
        line = folded[line_start : len(folded) if line_end < 0 else line_end].strip()
        if (
            not cut
            and end - start == 1
            and not folded[line_start:start].strip()
            and folded[end : end + 1] in (" ", "\t")
        ):
            continue  # 列表标记
        if line == m.group() and len(line) >= 3:
            continue  # 分隔线
        events.append((start, "∅" + folded[start:end]))
    return [tok for _, tok in sorted(events, key=lambda e: e[0])]


_PARENT: Final = {
    "table": {""},
    "thead": {"table"},
    "tbody": {"table"},
    "tr": {"table", "thead", "tbody"},
    "td": {"tr"},
    "th": {"tr"},
}


def _html_table_structure(md: str) -> list[str]:
    """HTML 表格必须严格配对嵌套、不自闭合、不嵌套表格，且单元格之外不得有文字。"""
    found: list[str] = []
    stack: list[str] = []
    pos = 0

    def stray(segment: str) -> None:
        if (
            stack
            and stack[-1] in {"table", "thead", "tbody", "tr"}
            and _BR.sub("", segment).strip()
        ):
            found.append("表格内单元格之外有文字")

    for m in _TABLE_TAG.finditer(md):
        stray(md[pos : m.start()])
        pos = m.end()
        closing, name, rest = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if rest.rstrip().endswith("/"):
            found.append(f"自闭合表格标签 <{name}/>")
            continue
        if closing:
            if not stack or stack[-1] != name:
                found.append(f"表格标签不配对 </{name}>")
            else:
                stack.pop()
        else:
            parent = stack[-1] if stack else ""
            if parent not in _PARENT[name]:
                found.append(f"表格标签嵌套位置不合法 <{name}>")
            stack.append(name)
    stray(md[pos:])
    if stack:
        found.append(f"表格标签未闭合 {stack}")
    return list(dict.fromkeys(found))


def _pipe_markup_violations(md: str) -> list[str]:
    found: list[str] = []
    for seg in _TABLE.split(md)[::2]:
        found.extend(_pipe_scan(seg)[1])
    return list(dict.fromkeys(found))


def markup_violations(md: str) -> list[str]:
    """入库的是 md，校验的是去标记后的文本；凡能让两者不一致的标记都在此拒绝。"""
    md = _nl(md)
    found: list[str] = []
    if any(unicodedata.category(c) == "Cf" for c in md):
        found.append("含不可见格式字符（Unicode Cf：方向控制符、零宽字符、软连字符等）")
    if "<!--" in md:
        found.append("HTML 注释")
    for span in _ANY_ANGLE.findall(md):
        if span.startswith("<!--"):
            continue
        if not _ALLOWED_TAG.fullmatch(span):
            found.append(f"不允许的标签或属性 {span[:40]!r}")
        elif any(c > 1 for c in Counter(n.lower() for n in _ATTR_NAME.findall(span)).values()):
            found.append(f"标签含重复属性 {span[:40]!r}")
    found.extend(_html_table_structure(md))
    if any(_BLANK_LINE.search(seg) for seg in _TABLE.split(md)[1::2]):
        found.append("HTML 表格内含空行（其后的 markdown 会被另行渲染）")
    if any("|" in seg for seg in _TABLE.split(md)[1::2]):
        found.append("HTML 表格内含竖线（可能是被另行渲染的 markdown 表）")
    found.extend(_pipe_markup_violations(md))
    for m in _ENTITY.finditer(md):
        tok = m.group(0)
        if tok not in _ENTITY_OK and html.unescape(tok) != tok:
            found.append(f"会被解码的字符实体 {tok!r}")
    return found


def _content_sequence(text: str) -> str:
    """内容字符（字母/数字/汉字）的有序序列，全角折叠后；标点与空白不计。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", fold_text(text)) if ch.isalnum())


def _stream_mismatch(a: str, b: str) -> bool:
    return critical_stream(a) != critical_stream(b)


def verify_against_text_layer(candidate_md: str, page_text: str) -> Verdict:
    candidate_md, page_text = _nl(candidate_md), _nl(page_text)
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
    if _content_sequence(plain) != _content_sequence(page_text):
        reasons.append("文字内容顺序与文字层不一致（标签/表头/主体名称换位或字词被改）")
    reasons.extend(table_structure_violations(candidate_md))
    return Verdict(not reasons, tuple(reasons))


@dataclass(frozen=True)
class _Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.text, self.rowspan, self.colspan)


def _cell_text(raw: str) -> str:
    return re.sub(r"\s+", "", fold_text(plain_text_from_markdown(raw)))


def _spans(tag: str) -> tuple[int, int]:
    got = {k.lower(): int(v) for k, v in _SPAN.findall(tag)}
    return got.get("rowspan", 1), got.get("colspan", 1)


def _html_table(table: str) -> list[list[_Cell]]:
    rows: list[list[_Cell]] = []
    for tr in _TR_SPLIT.split(table)[1:]:
        tags = _CELL_SPLIT.findall(tr)
        parts = _CELL_SPLIT.split(tr)[1:]
        rows.append([_Cell(_cell_text(p), *_spans(t)) for t, p in zip(tags, parts, strict=True)])
    return rows


def _is_separator(line: str) -> bool:
    chars = set(re.sub(r"\s+", "", line.replace("|", "")))
    return bool(chars) and chars <= {"-", ":"}


def _pipe_scan(segment: str) -> tuple[list[list[list[_Cell]]], list[str]]:
    """竖线表解析（GFM 语义）：以带前导竖线的行开始；表内每个非空行都是表行
    （前导竖线可省略，行宽不一会被结构检查抓到），只有空行才结束表。
    紧跟表头的含 `|` 的分隔行是定界行；之后再出现的分隔样式行按普通表行处理。

    违规（渲染结果与解析可能不一致）：表内转义竖线；缺少表头/前导竖线的分隔行。"""
    tables: list[list[list[_Cell]]] = []
    violations: list[str] = []
    current: list[list[_Cell]] | None = None
    delimiter_seen = False
    for line in segment.splitlines():
        s = line.strip()
        if current is None:
            if "|" in s and _is_separator(s):
                violations.append("竖线表分隔行前没有表头或缺少前导竖线")
                continue
            if not s.startswith("|"):
                continue
            current = []
            tables.append(current)
            delimiter_seen = False
        else:
            if not s:
                current = None
                continue
            if len(current) == 1 and not delimiter_seen and "|" in s and _is_separator(s):
                delimiter_seen = True
                continue
        if "\\|" in s:
            violations.append("竖线表内含转义竖线")
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        current.append([_Cell(_cell_text(c)) for c in s.split("|")])
    return tables, violations


def _parse_tables(md: str) -> list[list[list[_Cell]]]:
    tables: list[list[list[_Cell]]] = []
    for i, seg in enumerate(_TABLE.split(md)):
        if i % 2:
            tables.append(_html_table(seg))
        else:
            tables.extend(_pipe_scan(seg)[0])
    return tables


def table_cell_rows(md: str) -> list[tuple[tuple[str, int, int], ...]]:
    """文档顺序下所有表格行（含表头、含空单元格）：每格 (归一化文本, rowspan, colspan)。"""
    return [tuple(c.key for c in row) for t in _parse_tables(md) for row in t]


def table_structure_violations(md: str) -> list[str]:
    """文字层页上不允许的表格结构：空单元格/空行、rowspan/colspan（值 1 视为无）、行宽不一。

    这些结构能让数字在「文字顺序完全不变」的情况下落到另一列。"""
    found: list[str] = []
    for table in _parse_tables(md):
        if any(not row for row in table):
            found.append("表格含空行")
        if any(c.text == "" for row in table for c in row):
            found.append("表格含空单元格")
        if any(c.rowspan != 1 or c.colspan != 1 for row in table for c in row):
            found.append("表格含 rowspan/colspan")
        if len({len(row) for row in table}) > 1:
            found.append("表格各行单元格数不一致")
    return list(dict.fromkeys(found))


def verify_dual(primary_md: str, secondary_md: str) -> Verdict:
    """两个独立引擎互相对照，用于无文字层的页。

    已知局限（无法消除）：两个引擎犯相同的错误（相同的误识别、相同的漏行）时，
    互相对照无法发现——这里没有原文可比。该风险只能靠引擎独立性与抽样评测控制。

    要求：数字多重集相等；关键字多重集相等；数字+关键字阅读顺序序列相等；
    表格按行、按单元格、含表头完全相等；内容字符有序序列完全相等
    （两引擎文字排序方式不同会因此误报，T7 度量）；文字一致度 >= DUAL_MIN_COVERAGE；
    两路输出都不含隐藏内容的标记。"""
    primary_md, secondary_md = _nl(primary_md), _nl(secondary_md)
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
    if _content_sequence(a) != _content_sequence(b):
        reasons.append("文字内容顺序不一致（标签/表头/主体名称/子句换位或字词被改）")
    agree = min(text_coverage(_content(a), _content(b)), text_coverage(_content(b), _content(a)))
    if agree < DUAL_MIN_COVERAGE:
        reasons.append(f"文字一致度 {agree:.4f} < {DUAL_MIN_COVERAGE}")
    return Verdict(not reasons, tuple(reasons))
