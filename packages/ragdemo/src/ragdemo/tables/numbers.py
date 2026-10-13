"""单元格数字 → Decimal。分词复用 parse/local/number_closure.py（与 L0/L1 校验同一套口径）。

保守原则（CLAUDE.md §1.2）：数值转换全部由代码完成；含糊一律返回 None，绝不猜测。

两个层次：
- `cell_value(text)`：**消费者必须用这个**。整格只能陈述一个数（金额或百分数，可带一个行内单位）；
  多数字、区间、日期/年份语境、增减词、拆开的数字、括号序号一律 None。
- `token_to_decimal` / `cell_decimals`：**仅供诊断**，返回格内每个 token，不代表"这一格陈述的值"
  （年份、区间端点、"增加 16 . 70 个百分点" 等都会被拆成多个数）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from ragdemo.parse.local.number_closure import fold_text, number_spans

_INT: Final = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
_BODY: Final = rf"(?:{_INT}(?:\.[0-9]+)?|\.[0-9]+)"
# 括号负数：必须有小数或千分位结构；裸整数 "(1)" "(3000)" 是序号/注释，不是负数。
_PAREN_BODY: Final = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+\.[0-9]+|\.[0-9]+)"
_PAREN: Final = re.compile(rf"^\((?P<body>{_PAREN_BODY})(?P<pct_in>%?)\)(?P<pct_out>%?)$")
_PLAIN: Final = re.compile(rf"^(?P<neg>-?)(?P<body>{_BODY})(?P<pct>%?)$")

# 行内单位白名单（货币量/股数/计数）。"个百分点"、"年" 等不在其中 → 整格 None。
_UNITS: Final = (
    "百万元|千万元|万亿元|亿元|万元|千元|港元|欧元|日元|英镑|元"
    "|亿美元|万美元|千美元|美元|亿股|万股|股|万吨|吨|万人|人|万台|台|万件|件"
)
_WHOLE: Final = re.compile(
    rf"^(?P<num>-?{_BODY}%?|\({_PAREN_BODY}%?\)%?)(?:\s*(?P<unit>{_UNITS}))?$"
)
_BR: Final = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BR_SPLIT: Final = re.compile(r"[0-9.,]\s*<br\s*/?>\s*[0-9.,]", re.IGNORECASE)


def token_to_decimal(token: str) -> tuple[Decimal, bool] | None:
    """归一化 token → (值, 是否百分数)；括号 = 负数（仅限有小数/千分位结构）；含糊 → None。"""
    t = token.strip()
    m = _PAREN.match(t)
    if m:
        return -Decimal(m.group("body").replace(",", "")), bool(
            m.group("pct_in") or m.group("pct_out")
        )
    m = _PLAIN.match(t)
    if m:
        value = Decimal(m.group("body").replace(",", ""))
        return (-value if m.group("neg") else value), bool(m.group("pct"))
    return None


def cell_decimals(text: str) -> list[tuple[Decimal, bool]]:
    """诊断用：格内所有 token。**不要**用它判断"这一格的值"，用 `cell_value`。"""
    folded = fold_text(text)
    # 用原文跨度（保留千分位逗号），括号负数的"小数/千分位结构"判断才有依据
    raws = ("".join(folded[a:b].split()) for a, b, _ in number_spans(text))
    return [v for raw in raws if (v := token_to_decimal(raw)) is not None]


@dataclass(frozen=True)
class CellNumber:
    value: Decimal
    kind: Literal["amount", "percent"]
    inline_unit: str | None
    text: str  # 去掉 <br> 与首尾空白后的展示文本


def cell_value(text: str) -> CellNumber | None:
    """整格陈述的唯一数值；任何含糊 → None。

    - 整格匹配：一个数字（可带 -、千分位、小数、百分号、括号负数），后面至多一个白名单行内单位。
    - `<br>` 只用于展示被去掉；但数字之间被 `<br>` 拆开（"649,900,00<br>0"）→ None。
    - 增减词、年/月/日、区间、多个数字、空格拆开的数字、货币符号前缀 → None。
    - 裸年份 "2024" 无语境可辨，按金额返回；调用方需结合列表头判断。
    """
    if _BR_SPLIT.search(text):
        return None
    shown = " ".join(_BR.sub(" ", text).split())
    folded = fold_text(shown)
    m = _WHOLE.match(folded)
    if m is None:
        return None
    unit = m.group("unit")
    parsed = token_to_decimal(m.group("num"))
    if parsed is None:
        return None
    value, pct = parsed
    if pct and unit:
        return None
    return CellNumber(value, "percent" if pct else "amount", unit, shown)
