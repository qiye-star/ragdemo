"""引擎输出的确定性标记归一化：只改标签外壳，不动任何文字、数字或单元格内容。

PaddleOCR 的表格是 `<div style=..><html><body><table border="1">...`；verify.markup_violations
只放行 table/thead/tbody/tr/td/th/br 与 rowspan/colspan。这里把包装标签换成一个空格（保留内部
内容，且绝不零宽，否则相邻块的数字会粘在一起）、丢掉展示性属性、标签名小写。

保守原则：标签必须严格匹配（`<` 后不得有空白，标签名后必须是空白、`/` 或 `>`，名字里有 `-`/`:`
的不是标签）；属性必须能被干净切分成 name=value（带引号或不带）；任何不能确定的标签原样保留，
让校验器去拒绝。注释整体原样保留。非表格标签（如 <b>）也原样保留。"""

from __future__ import annotations

import re

_TOKEN = re.compile(
    r"<!--.*?-->|<(/?)([A-Za-z][A-Za-z0-9]*)(?=[ \t\n\r\f/>])((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>",
    re.DOTALL,
)
_ATTR = re.compile(
    r"[ \t\n\r\f]+([^ \t\n\r\f\"'<>/=]+)[ \t\n\r\f]*=[ \t\n\r\f]*"
    r"(\"[^\"]*\"|'[^']*'|[^ \t\n\r\f\"'<>=`/]+)"
)
# 被去掉的包装标签的占位；最后统一折成单个空格，或在标签/换行/首尾边界处丢弃。
_HTML_WS = " " + chr(9) + chr(10) + chr(13) + chr(12)  # HTML 空白（不含 NBSP、U+3000）
_GAP = chr(0)
_GAP_RUN = re.compile(chr(0) + "+")
_WRAPPERS = frozenset({"div", "html", "body", "head"})
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tr", "td", "th"})


def _attributes(rest: str) -> list[tuple[str, str]] | None:
    """切分属性串；任何无法干净切分的情况（无值属性、引号不配对、多余字符）返回 None。"""
    pairs: list[tuple[str, str]] = []
    pos = 0
    while True:
        m = _ATTR.match(rest, pos)
        if not m:
            break
        pairs.append((m.group(1), m.group(2).strip("\"'")))
        pos = m.end()
    return pairs if rest[pos:].strip(_HTML_WS) == "" else None


def _rewrite(m: re.Match[str]) -> str:
    whole = m.group(0)
    if whole.startswith("<!--"):
        return whole
    closing, name, rest = m.group(1), m.group(2).lower(), m.group(3)
    if name not in _WRAPPERS and name not in _TABLE_TAGS:
        return whole
    if closing:
        if rest.strip(_HTML_WS):
            return whole
        return _GAP if name in _WRAPPERS else f"</{name}>"
    attrs = _attributes(rest)
    if attrs is None:
        return whole
    if name in _WRAPPERS:
        return _GAP
    kept: list[str] = []
    for key, value in attrs:
        if key.lower() in {"rowspan", "colspan"}:
            if name not in {"td", "th"} or not (value.isascii() and value.isdigit()):
                return whole
            kept.append(f' {key.lower()}="{value}"')
    return f"<{name}{''.join(kept)}>"


def _settle(m: re.Match[str]) -> str:
    """占位折成一个空格；紧邻标签、换行或首尾时丢弃（那里本来就是边界，不会粘连内容）。"""
    s, e, text = m.start(), m.end(), m.string
    before = text[s - 1] if s else "\n"
    after = text[e] if e < len(text) else "\n"
    return "" if before in ">\n" or after in "<\n" else " "


def normalize_markup(markdown: str) -> str:
    if _GAP in markdown:  # 输入里本来就有 NUL：不做任何改写，交给校验器
        return markdown
    return _GAP_RUN.sub(_settle, _TOKEN.sub(_rewrite, markdown))
