"""不进入检索索引的内容。

过滤过度会丢召回，因此规则保守且可配置，变更必须跑检索评测（05 §3.3）。
"""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^第\s*\d+\s*页\s*共\s*\d+\s*页$"),
    re.compile(r"^-?\s*\d+\s*-?$"),  # 孤立页码
    re.compile(r"^目\s*录$"),
    re.compile(r"^释\s*义$"),
    re.compile(r"保证信息披露内容的真实、准确和完整"),  # 各家雷同的免责模板
    re.compile(r"^本报告期内公司不存在.{0,20}情形$"),
)


def is_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(p.search(stripped) for p in _PATTERNS)
