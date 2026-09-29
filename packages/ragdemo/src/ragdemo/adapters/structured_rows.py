"""阶段二三类结构化数据的归一化行（适配器产出、写入器消费）。

放在 adapters 包里，让适配器与写入器（ingest）都只依赖这一个纯数据模块，
适配器不必 import 写库代码。金额一律元、比例一律 0~1 小数、股数一律股。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class HoldingRow:
    rank: int
    holder_name: str
    shares: Decimal
    ratio: Decimal


@dataclass(frozen=True)
class SegmentRow:
    dimension: str  # 'product' | 'region' | 'industry'
    name: str
    revenue: Decimal
    gross_profit: Decimal | None
    share: Decimal | None


@dataclass(frozen=True)
class FxRow:
    base_ccy: str  # 1 单位 base = rate 单位 quote
    quote_ccy: str
    rate_date: date
    rate: Decimal
    known_at: datetime
