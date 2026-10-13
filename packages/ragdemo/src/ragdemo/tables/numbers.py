"""单元格数字 → Decimal。分词复用 parse/local/number_closure.py（与 L0/L1 校验同一套口径）。

保守原则（CLAUDE.md §1.2）：token 必须严格匹配 ASCII 十进制语法，
任何含糊（括号内再带负号、NaN/Infinity/科学计数法、残缺括号）一律返回 None，绝不猜测。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from ragdemo.parse.local.number_closure import ordered_numbers

# 括号形态 "(3.5%)" 的百分号在括号内；"(3.5)%" 已被分词器归一化为 "(3.5%)"。
_BODY: Final = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_PAREN: Final = re.compile(rf"^\((?P<body>{_BODY})(?P<pct>%?)\)$")
_PLAIN: Final = re.compile(rf"^(?P<neg>-?)(?P<body>{_BODY})(?P<pct>%?)$")


def token_to_decimal(token: str) -> tuple[Decimal, bool] | None:
    """归一化 token → (值, 是否百分数)；括号 = 负数；含糊 → None。"""
    t = token.strip()
    m = _PAREN.match(t)
    if m:
        return -Decimal(m.group("body")), bool(m.group("pct"))
    m = _PLAIN.match(t)
    if m:
        value = Decimal(m.group("body"))
        return (-value if m.group("neg") else value), bool(m.group("pct"))
    return None


def cell_decimals(text: str) -> list[tuple[Decimal, bool]]:
    return [v for tok in ordered_numbers(text) if (v := token_to_decimal(tok)) is not None]
