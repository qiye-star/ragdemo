# packages/ragdemo/src/ragdemo/parse/tiered/ledger.py
# ruff: noqa: E501
"""逐页台账：落库、费用估算、按时间窗汇总、告警（L0 + TextIn 分页接入计划 T7）。

单价不写死：调用方传 RagdemoConfig.textin_cost_per_page_cny；缺单价时 cost_cny 记 NULL，报表按当时
单价重算，仍缺则计入"未定价页数"，不编一个数。月度花费的权威是 provider_snapshot（预算闸门读它）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

import psycopg

from ragdemo.ops.alerts import Alert, Severity, raise_alert
from ragdemo.parse.tiered.records import (
    LEDGER_TIERS,
    REASON_L3_NO_OUTPUT,
    LedgerTier,
    PageRecord,
    ParseLedger,
)

SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
L3_SHARE_ALERT: Final = 0.85
L3_SHARE_MIN_PAGES: Final = 50
PARSE_SPEND_ALERT_SHARE: Final = Decimal("0.30")  # ADR-0008 推翻条件：解析支出 ≥ 总预算 30%


def estimate_page_cost(rec: PageRecord, textin_per_page: Decimal | None) -> Decimal | None:
    if not rec.textin_billed:
        return Decimal(0)
    return textin_per_page


def _checks_json(rec: PageRecord) -> str:
    return json.dumps(
        {
            cid: {"passed": ok, "state": state, "detail": detail}
            for cid, ok, state, detail in rec.checks
        },
        ensure_ascii=False,
    )


def record_parse_ledger(
    conn: psycopg.Connection,
    *,
    ledger: ParseLedger,
    content_hash: str,
    doc_type: str,
    ingest_run_id: str,
    routed_at: datetime,
    textin_per_page: Decimal | None,
) -> int:
    inserted = 0
    for r in ledger.pages:
        cur = conn.execute(
            "INSERT INTO core.parse_page_route (routes_ref, content_hash, doc_type, page, tier, engine,"
            " page_kind, reasons, skip_rule, shadow_skip_rules, textin_billed, textin_json_ref, tier_verdict,"
            " p3_state, unverified_numbers, output_numbers, image_ocr_numbers, coverage_ratio, table_blocks,"
            " figure_count, chart_regions, acceptance_checks, cost_cny, ingest_run_id, routed_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)"
            " ON CONFLICT (routes_ref, page) DO NOTHING",
            (
                ledger.routes_ref,
                content_hash,
                doc_type,
                r.page,
                r.tier,
                r.engine,
                r.page_kind,
                list(r.reasons),
                r.skip_rule,
                list(r.shadow_skip_rules),
                r.textin_billed,
                r.textin_json_ref,
                r.tier_verdict,
                r.p3_state,
                r.unverified_numbers,
                r.output_numbers,
                r.image_ocr_numbers,
                r.coverage_ratio,
                r.table_blocks,
                r.figure_count,
                r.chart_regions,
                _checks_json(r),
                estimate_page_cost(r, textin_per_page),
                ingest_run_id,
                routed_at,
            ),
        )
        inserted += cur.rowcount
    d = ledger.doc
    conn.execute(
        "INSERT INTO core.parse_doc_acceptance (routes_ref, content_hash, doc_type, state, reasons, warnings,"
        " unverified_numbers, scan_pages, page_count, l0_pages, l3_pages, skipped_pages, billed_pages,"
        " fallback_reason, acceptance_version, ingest_run_id, evaluated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (routes_ref) DO UPDATE SET state = EXCLUDED.state, reasons = EXCLUDED.reasons,"
        " warnings = EXCLUDED.warnings, unverified_numbers = EXCLUDED.unverified_numbers,"
        " scan_pages = EXCLUDED.scan_pages, acceptance_version = EXCLUDED.acceptance_version,"
        " evaluated_at = EXCLUDED.evaluated_at",
        (
            ledger.routes_ref,
            content_hash,
            doc_type,
            d.state,
            list(d.reasons),
            list(d.warnings),
            d.unverified_numbers,
            d.scan_pages,
            d.page_count,
            d.l0_pages,
            d.l3_pages,
            d.skipped_pages,
            d.billed_pages,
            d.fallback_reason,
            d.acceptance_version,
            ingest_run_id,
            routed_at,
        ),
    )
    return inserted


@dataclass(frozen=True)
class TierRow:
    tier: LedgerTier
    pages: int
    billed_pages: int
    cost_cny: Decimal
    pages_unpriced: int


def tier_summary(
    conn: psycopg.Connection,
    *,
    start: datetime,
    end: datetime,
    textin_per_page: Decimal | None,
    doc_type: str | None = None,
) -> list[TierRow]:
    rows = conn.execute(
        "SELECT tier, count(*), count(*) FILTER (WHERE textin_billed), coalesce(sum(cost_cny), 0),"
        "       count(*) FILTER (WHERE textin_billed AND cost_cny IS NULL)"
        "  FROM core.parse_page_route"
        " WHERE routed_at >= %s AND routed_at < %s AND (%s::text IS NULL OR doc_type = %s)"
        " GROUP BY tier",
        (start, end, doc_type, doc_type),
    ).fetchall()
    got = {str(r[0]): r for r in rows}
    out: list[TierRow] = []
    for t in LEDGER_TIERS:
        r = got.get(t)
        if r is None:
            out.append(TierRow(t, 0, 0, Decimal(0), 0))
            continue
        cost, unpriced = Decimal(r[3]), int(r[4])
        if unpriced and textin_per_page is not None:
            cost, unpriced = cost + textin_per_page * unpriced, 0
        out.append(TierRow(t, int(r[1]), int(r[2]), cost, unpriced))
    return out


def format_tier_summary(rows: Sequence[TierRow]) -> str:
    lines = ["| 去向 | 页数 | 计费页 | 估算费用（元） | 未定价页 |", "|---|---|---|---|---|"]
    lines += [
        f"| {r.tier} | {r.pages} | {r.billed_pages} | {r.cost_cny:.4f} | {r.pages_unpriced} |"
        for r in rows
    ]
    lines.append(
        f"| 合计 | {sum(r.pages for r in rows)} | {sum(r.billed_pages for r in rows)}"
        f" | {sum((r.cost_cny for r in rows), Decimal(0)):.4f} | {sum(r.pages_unpriced for r in rows)} |"
    )
    return "\n".join(lines)


def l3_no_output_alerts(ledger: ParseLedger) -> list[Alert]:
    """L3 页没有 TextIn JSON / 输出为空：该页数字无处可溯源，必须有人看（每份文档一条告警）。"""
    pages = [r.page for r in ledger.pages if r.tier == "L3" and REASON_L3_NO_OUTPUT in r.reasons]
    if not pages:
        return []
    shown = ", ".join(str(p) for p in pages[:20]) + (" ..." if len(pages) > 20 else "")
    return [
        Alert(
            "parse_ledger",
            "warning",
            f"parse_ledger:l3_no_output:{ledger.routes_ref}",
            f"{len(pages)} 个 L3 页没有 TextIn 输出",
            f"页：{shown}；查 {ledger.routes_ref} 与对应 TextIn JSON（缓存是否损坏、是否被截断）",
        )
    ]


def evaluate_ledger_alerts(
    day_rows: Sequence[TierRow],
    *,
    fallback_docs: int,
    l0_unverified_pages: int,
    month_textin_cny: Decimal,
    monthly_budget_cny: Decimal,
) -> list[Alert]:
    alerts: list[Alert] = []
    if l0_unverified_pages > 0:
        alerts.append(
            Alert(
                "parse_ledger",
                "critical",
                "parse_ledger:l0_unverified",
                f"近 24 小时有 {l0_unverified_pages} 个 L0 页数字未能溯源",
                "停止条件 S-A：立即设 PARSE_TIER_L0_ENABLED=false，排查排版器",
            )
        )
    if fallback_docs > 0:
        alerts.append(
            Alert(
                "parse_ledger",
                "warning",
                "parse_ledger:fallback",
                f"近 24 小时有 {fallback_docs} 份文档整份退回 TextIn",
                "查 core.parse_doc_acceptance.fallback_reason",
            )
        )
    by = {r.tier: r.pages for r in day_rows}
    routed = by.get("L0", 0) + by.get("L3", 0)
    if routed >= L3_SHARE_MIN_PAGES and by.get("L3", 0) / routed > L3_SHARE_ALERT:
        alerts.append(
            Alert(
                "parse_ledger",
                "warning",
                "parse_ledger:l3_share",
                f"近 24 小时 L3 占比 {by.get('L3', 0) / routed:.0%} 超过 {L3_SHARE_ALERT:.0%}",
                f"L3 {by.get('L3', 0)} 页 / 路由 {routed} 页；L0 可能没在工作，成本不降",
            )
        )
    if month_textin_cny >= monthly_budget_cny * PARSE_SPEND_ALERT_SHARE:
        severity: Severity = "critical" if month_textin_cny >= monthly_budget_cny else "warning"
        alerts.append(
            Alert(
                "parse_ledger",
                severity,
                "parse_ledger:budget",
                f"本月 TextIn 花费 {month_textin_cny:.2f} 元，达总预算的"
                f" {month_textin_cny / monthly_budget_cny:.0%}",
                "ADR-0008 推翻条件：解析支出超过总预算 30%",
            )
        )
    return alerts


def collect_ledger_alerts(
    conn: psycopg.Connection,
    *,
    now: datetime,
    textin_per_page: Decimal | None,
    monthly_budget_cny: Decimal,
) -> list[Alert]:
    since = now - timedelta(hours=24)
    day = tier_summary(conn, start=since, end=now, textin_per_page=textin_per_page)
    fb = conn.execute(
        "SELECT count(*) FROM core.parse_doc_acceptance"
        " WHERE evaluated_at >= %s AND evaluated_at < %s AND fallback_reason IS NOT NULL",
        (since, now),
    ).fetchone()
    bad = conn.execute(
        "SELECT count(*) FROM core.parse_page_route"
        " WHERE routed_at >= %s AND routed_at < %s AND tier = 'L0' AND p3_state <> 'pass'",
        (since, now),
    ).fetchone()
    month_start = now.astimezone(SHANGHAI).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = conn.execute(
        "SELECT coalesce(sum(cost_cents), 0) / 100 FROM core.provider_snapshot"
        " WHERE provider = 'textin' AND fetched_at >= %s AND fetched_at < %s",
        (month_start, now),
    ).fetchone()
    return evaluate_ledger_alerts(
        day,
        fallback_docs=int(fb[0]) if fb else 0,
        l0_unverified_pages=int(bad[0]) if bad else 0,
        month_textin_cny=Decimal(spent[0]) if spent else Decimal(0),
        monthly_budget_cny=monthly_budget_cny,
    )


def emit_ledger_alerts(
    conn: psycopg.Connection,
    *,
    now: datetime,
    textin_per_page: Decimal | None,
    monthly_budget_cny: Decimal,
) -> list[dict[str, str]]:
    alerts = collect_ledger_alerts(
        conn, now=now, textin_per_page=textin_per_page, monthly_budget_cny=monthly_budget_cny
    )
    return [raise_alert(conn, a, now=now) for a in alerts]
