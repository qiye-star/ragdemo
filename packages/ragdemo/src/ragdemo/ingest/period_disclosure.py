"""报告期首次披露时刻（阶段二 Task 2）。

Wind 的股东 / 分部数据只给"当前值"、不给披露日，`known_at` 只能借公告目录
`core.announcement_index` 回填：该期定期报告**最早**何时可获知。负责人 2026-09-29
决定：同一期有多份（更正）报告时取**第一次披露日**——所以这里取 `min(known_at)`，
更正版不可能更早，也就不必排除它们（排除反而会在原版缺失时误丢）。

只认主源（巨潮、披露易）的行，且**包含已被取代的行**：被取代只表示来源换了，
不影响"最早何时可获知"；iFind 只是片段，不作数。匹配不到返回 `None`——调用方不
入库并记警告，不伪造 `known_at`（与 `adapters/wind.py` 同一规则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import psycopg

_PERIOD = re.compile(r"^(\d{4})(FY|H1|Q1|Q3)$")
_PERIOD_END = {"FY": (12, 31), "H1": (6, 30), "Q1": (3, 31), "Q3": (9, 30)}
# 期别 → 标题里的报告名。`年度报告` 前面不能是"半"（避开"半年度报告"）。
_REPORT_NAME = {
    "FY": r"(?<!半)年度报告",
    "H1": r"半年度报告",
    "Q1": r"第一季度报告",
    "Q3": r"第三季度报告",
}
_EXCLUDED = re.compile(r"英文版|English|取消|提示性")
# 报告期结束后合理的披露窗口：晚于此的多半是往年报告的重发/转载。
_MAX_MONTHS_AFTER_PERIOD_END = 15


@dataclass(frozen=True)
class Disclosure:
    known_at: datetime
    index_id: int
    title: str


def period_end_of(period: str) -> date:
    m = _PERIOD.match(period)
    if m is None:
        raise ValueError(f"不支持的期别 {period!r}（应为 2024FY / 2025H1 / 2025Q1 / 2025Q3）")
    month, day = _PERIOD_END[m.group(2)]
    return date(int(m.group(1)), month, day)


def _add_months(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(d.day, 28))


def first_disclosure(conn: psycopg.Connection, entity_id: str, period: str) -> Disclosure | None:
    m = _PERIOD.match(period)
    if m is None:
        raise ValueError(f"不支持的期别 {period!r}")
    year, kind = m.group(1), m.group(2)
    end = period_end_of(period)
    title_re = re.compile(f"{year}年{_REPORT_NAME[kind]}")
    rows = conn.execute(
        "SELECT index_id, title, known_at FROM core.announcement_index"
        " WHERE entity_id = %s AND source IN ('cninfo', 'hkexnews')"
        "   AND publish_date > %s AND publish_date <= %s"
        " ORDER BY known_at, index_id",
        (entity_id, end, _add_months(end, _MAX_MONTHS_AFTER_PERIOD_END)),
    ).fetchall()
    for index_id, title, known_at in rows:
        if _EXCLUDED.search(title) or title_re.search(title) is None:
            continue
        return Disclosure(known_at=known_at, index_id=int(index_id), title=str(title))
    return None
