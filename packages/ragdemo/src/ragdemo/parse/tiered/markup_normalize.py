"""引擎输出的确定性标记归一化：只改标签外壳，不动任何文字、数字或单元格内容。

PaddleOCR 的表格是 `<div style=..><html><body><table border="1">...`；verify.markup_violations
只放行 table/thead/tbody/tr/td/th/br 与 rowspan/colspan。这里把包装标签去掉（保留内部内容）、
丢掉展示性属性、标签名小写。非表格标签（如 <b>）原样保留，由校验器去拒绝。"""

from __future__ import annotations

import re

_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_SPAN_ATTR = re.compile(r"\b(rowspan|colspan)\s*=\s*(?:\"(\d+)\"|'(\d+)'|(\d+))", re.IGNORECASE)
_WRAPPERS = frozenset({"div", "html", "body", "head"})
_TABLE_TAGS = frozenset({"table", "thead", "tbody", "tr", "td", "th"})


def _rewrite(m: re.Match[str]) -> str:
    closing, name, rest = m.group(1), m.group(2).lower(), m.group(3)
    if name in _WRAPPERS:
        return ""
    if name not in _TABLE_TAGS:
        return m.group(0)
    if closing:
        return f"</{name}>"
    if rest.rstrip().endswith("/"):
        return m.group(0)  # 自闭合：保持原样，让校验器拒绝
    attrs = "".join(
        f' {a.group(1).lower()}="{a.group(2) or a.group(3) or a.group(4)}"'
        for a in _SPAN_ATTR.finditer(rest)
        if name in {"td", "th"}
    )
    return f"<{name}{attrs}>"


def normalize_markup(markdown: str) -> str:
    return _TAG.sub(_rewrite, markdown)
