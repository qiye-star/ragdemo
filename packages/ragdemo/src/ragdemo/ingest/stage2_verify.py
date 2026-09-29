"""阶段二验收：股东 / 分部 / 汇率入库后的数据体检（`ragdemo facts verify-stage2`）。

只读库，不调用任何外部服务。每条检查都对着"数据不出错"这个验收标准设计：

1. 股东：每个 (实体, 期别) 名次连续从 1 起、持股数量按名次递减、比例之和 ≤ 100%；
2. 分部：每个维度的收入之和与 `fin_fact` 该期营业收入相差 ≤ 0.5%（库里没有该期营收的
   记为"未对账"，不算错但要报告）；
3. 时点：每一行的 `known_at` **不早于**该期定期报告的首次披露时刻；`first_periodic_report`
   的行**恰好等于**它；`revision_observed` 的行晚于它；
4. 溯源：每一行的 `snapshot_id` 存在，且快照里真有原始响应（`response` 或 `response_ref`）；
5. 汇率：数值落在各币种的合理区间（捕捉 100JPY / CNY/KRW 口径换算错误）、相邻交易日波动
   ≤ 5%（捕捉倒数写反）、相邻记录间隔 ≤ 12 天、且各币种对的交易日集合一致（CFETS 对所有
   币种同日公布，某一对独有的缺口就是漏取）；
6. 复现：同一个 `as_of` 连续查两次，三张表的结果逐行完全一致；
7. 财务事实（阶段 2.6）：每条 Wind 财务事实的 `known_at`（Wind 自报的"实际披露日期"）与公告目录
   里该期定期报告的首次披露日（北京时间的日历日）一致——两个互相独立的来源互相印证；
   累计口径下 `Q2` 即半年报（H1）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from typing import Literal

import psycopg

from ragdemo.ingest.period_disclosure import first_disclosure
from ragdemo_core.db.session import as_of_session

# 各币种兑人民币的合理区间（1 单位外币折人民币）——远宽于 2023 年以来的真实波动，只挡量纲错误。
FX_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "USD": (Decimal("5"), Decimal("9")),
    "EUR": (Decimal("5"), Decimal("11")),
    "HKD": (Decimal("0.7"), Decimal("1.2")),
    "JPY": (Decimal("0.03"), Decimal("0.09")),
    "KRW": (Decimal("0.003"), Decimal("0.009")),
}
MAX_DAILY_MOVE = Decimal("0.05")
# 春节 / 国庆连调休最长休市 11 天（实测 2023-09-28→10-09、2026-02-13→02-24），留 1 天余量。
MAX_GAP_DAYS = 12
REVENUE_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    check: str
    message: str


def verify_stage2(conn: psycopg.Connection, *, as_of: datetime) -> list[Finding]:
    findings: list[Finding] = []
    findings += _check_holdings(conn)
    findings += _check_segments(conn, as_of)
    findings += _check_known_at(conn)
    findings += _check_snapshots(conn)
    findings += _check_fx(conn)
    findings += _check_fin_fact_known_at(conn)
    findings += _check_reproducible(conn, as_of)
    return findings


def _check_holdings(conn: psycopg.Connection) -> list[Finding]:
    out: list[Finding] = []
    rows = conn.execute(
        "SELECT entity_id, period, rank, shares, ratio FROM core.shareholder_holding"
        " WHERE superseded_at IS NULL ORDER BY entity_id, period, rank"
    ).fetchall()
    groups: dict[tuple[str, str], list[tuple[int, Decimal, Decimal]]] = {}
    for entity_id, period, rank, shares, ratio in rows:
        groups.setdefault((entity_id, period), []).append((rank, shares, ratio))
    for (entity_id, period), items in groups.items():
        key = f"{entity_id}/{period}"
        if [r for r, _, _ in items] != list(range(1, len(items) + 1)):
            out.append(
                Finding("error", "holdings", f"{key}：名次不连续 {[r for r, _, _ in items]}")
            )
        if any(a[1] < b[1] for a, b in pairwise(items)):
            out.append(Finding("error", "holdings", f"{key}：持股数量没有按名次递减"))
        if sum(r for _, _, r in items) > Decimal("1.001"):
            out.append(Finding("error", "holdings", f"{key}：比例之和超过 100%"))
    return out


def _fin_revenue(
    conn: psycopg.Connection, entity_id: str, period: str, as_of: datetime
) -> Decimal | None:
    fin_period = f"{period[:4]}Q2" if period.endswith("H1") else period
    with as_of_session(conn, as_of):
        row = conn.execute(
            "SELECT value FROM asof.fin_fact WHERE entity_id = %s AND metric_id = 'revenue_total'"
            " AND period = %s",
            (entity_id, fin_period),
        ).fetchone()
    return Decimal(row[0]) if row else None


def _check_segments(conn: psycopg.Connection, as_of: datetime) -> list[Finding]:
    out: list[Finding] = []
    rows = conn.execute(
        "SELECT entity_id, period, dimension, sum(revenue) FROM core.segment_revenue"
        " WHERE superseded_at IS NULL GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ).fetchall()
    for entity_id, period, dimension, total in rows:
        key = f"{entity_id}/{period}/{dimension}"
        revenue = _fin_revenue(conn, entity_id, period, as_of)
        if revenue is None:
            out.append(Finding("warning", "segments", f"{key}：库里没有该期营业收入，未对账"))
        elif abs(Decimal(total) - revenue) / revenue > REVENUE_TOLERANCE:
            out.append(
                Finding(
                    "error", "segments", f"{key}：分部收入之和 {total} 与营业收入 {revenue} 不符"
                )
            )
    return out


def _check_known_at(conn: psycopg.Connection) -> list[Finding]:
    out: list[Finding] = []
    for table in ("core.shareholder_holding", "core.segment_revenue"):
        rows = conn.execute(
            f"SELECT DISTINCT entity_id, period, known_at, known_at_basis FROM {table}"
            " ORDER BY 1, 2, 3"
        ).fetchall()
        for entity_id, period, known_at, basis in rows:
            disc = first_disclosure(conn, entity_id, period)
            key = f"{table}:{entity_id}/{period}"
            if disc is None:
                out.append(Finding("error", "known_at", f"{key}：公告目录里找不到该期定期报告"))
            elif basis == "first_periodic_report" and known_at != disc.known_at:
                out.append(
                    Finding(
                        "error",
                        "known_at",
                        f"{key}：known_at {known_at} ≠ 首次披露 {disc.known_at}",
                    )
                )
            elif known_at < disc.known_at:
                out.append(
                    Finding(
                        "error",
                        "known_at",
                        f"{key}：known_at {known_at} 早于首次披露 {disc.known_at}",
                    )
                )
    return out


def _check_snapshots(conn: psycopg.Connection) -> list[Finding]:
    out: list[Finding] = []
    for table in ("core.shareholder_holding", "core.segment_revenue", "core.fx_rate"):
        row = conn.execute(
            f"SELECT count(*) FROM {table} t LEFT JOIN core.provider_snapshot s USING (snapshot_id)"
            " WHERE s.snapshot_id IS NULL OR (s.response IS NULL AND s.response_ref IS NULL)"
        ).fetchone()
        assert row is not None
        if row[0]:
            out.append(Finding("error", "snapshots", f"{table}：{row[0]} 行的原始响应不可回放"))
    return out


def _check_fx(conn: psycopg.Connection) -> list[Finding]:
    out: list[Finding] = []
    rows = conn.execute(
        "SELECT base_ccy, rate_date, rate FROM core.fx_rate"
        " WHERE superseded_at IS NULL AND quote_ccy = 'CNY' ORDER BY base_ccy, rate_date"
    ).fetchall()
    series: dict[str, list[tuple[date, Decimal]]] = {}
    for base, rate_date, rate in rows:
        series.setdefault(base, []).append((rate_date, Decimal(rate)))
    for base, points in series.items():
        lo, hi = FX_RANGES.get(base, (Decimal(0), Decimal("1e9")))
        for rate_date, rate in points:
            if not lo <= rate <= hi:
                out.append(Finding("error", "fx", f"{base}/CNY {rate_date}：{rate} 超出合理区间"))
        for (d0, r0), (d1, r1) in pairwise(points):
            if abs(r1 - r0) / r0 > MAX_DAILY_MOVE:
                out.append(
                    Finding("error", "fx", f"{base}/CNY {d0}→{d1}：波动 {abs(r1 - r0) / r0:.1%}")
                )
            gap = (d1 - d0).days
            if gap > MAX_GAP_DAYS:
                out.append(Finding("error", "fx", f"{base}/CNY {d0}→{d1}：间隔 {gap} 天，疑似漏取"))
    dates = {base: {d for d, _ in points} for base, points in series.items()}
    if dates:
        union = set().union(*dates.values())
        for base, have in dates.items():
            missing = union - have
            if missing:
                out.append(
                    Finding(
                        "error",
                        "fx",
                        f"{base}/CNY 缺 {len(missing)} 个其它币种对都有的交易日，如 {min(missing)}",
                    )
                )
    return out


_CST = timezone(timedelta(hours=8))


def _check_fin_fact_known_at(conn: psycopg.Connection) -> list[Finding]:
    """财务事实的 known_at（Wind 自报披露日）对公告目录的首次披露日——按北京时间日历日比。"""
    out: list[Finding] = []
    rows = conn.execute(
        "SELECT DISTINCT entity_id, period, known_at FROM core.fin_fact"
        " WHERE superseded_at IS NULL AND source = 'wind' ORDER BY 1, 2"
    ).fetchall()
    for entity_id, period, known_at in rows:
        key = f"{entity_id}/{period}"
        disc = first_disclosure(conn, entity_id, str(period).replace("Q2", "H1"))
        if disc is None:
            out.append(
                Finding(
                    "warning", "fin_fact_known_at", f"{key}：公告目录里找不到该期报告，无法互证"
                )
            )
        elif disc.known_at.astimezone(_CST).date() != known_at.astimezone(_CST).date():
            out.append(
                Finding(
                    "error",
                    "fin_fact_known_at",
                    f"{key}：Wind 披露日 {known_at.astimezone(_CST).date()} ≠ 公告目录首次披露 "
                    f"{disc.known_at.astimezone(_CST).date()}",
                )
            )
    return out


def _digest(conn: psycopg.Connection, as_of: datetime) -> str:
    h = hashlib.sha256()
    with as_of_session(conn, as_of):
        for query in (
            "SELECT entity_id, period, rank, holder_name, shares, ratio, known_at"
            " FROM asof.shareholder_holding ORDER BY 1, 2, 3",
            "SELECT entity_id, period, dimension, segment_name, revenue, known_at"
            " FROM asof.segment_revenue ORDER BY 1, 2, 3, 4",
            "SELECT base_ccy, quote_ccy, rate_date, rate, known_at"
            " FROM asof.fx_rate ORDER BY 1, 2, 3",
        ):
            for row in conn.execute(query):
                h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def _check_reproducible(conn: psycopg.Connection, as_of: datetime) -> list[Finding]:
    if _digest(conn, as_of) != _digest(conn, as_of):
        return [Finding("error", "reproducible", "同一 as_of 两次查询结果不一致")]
    return []
