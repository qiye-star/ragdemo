"""Wind 漏收核对（A 股，公告双源接入计划 Task 11）。

对每家 A 股实体按月问 Wind（`WindDocsAdapter`，经 `Capability.ANNOUNCEMENTS_VERIFY`
路由——`ROUTES` 里这条能力只有一层，`writes=False`：这里只核对、不写目录，原始
响应仍然要留 `provider_snapshot`，CLAUDE.md §1.5 对"核对"调用与"写入"调用一视同仁），
与目录里现行的 cninfo 行按 `(publish_date ±1 日, title_norm)` 比对：Wind 有、目录里
找不到匹配 → 疑似漏收，计入 `quality.quality_metric`（`announcement_coverage_gap`，
阈值 0）。

范围明确限定 A 股。港股走披露易、美股走 EDGAR，这两个都是官方指定的披露平台
本身，不存在"巨潮之外还漏了什么"这个问题需要核对——核对的前提是"官方渠道之外
还有一个可能更全的参照物"，A 股才有这个参照物（Wind）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.wind_docs import WindDocsAdapter
from ragdemo.ingest.announcement_index import normalize_title
from ragdemo.ingest.snapshot import record_snapshot
from ragdemo.quality.metrics import MetricResult, record_metric
from ragdemo_core.blob import BlobStore

METRIC_NAME = "announcement_coverage_gap"

# 只有这几个市场走巨潮资讯网这条主链——见 Capability.ANNOUNCEMENTS_CN 的分工。
_CN_MARKETS = ("SSE", "SZSE", "BSE")


@dataclass(frozen=True)
class CoverageGap:
    entity_ref: str
    title: str
    publish_date: date


@dataclass(frozen=True)
class _CnEntity:
    entity_id: str
    tushare_code: str
    short_names: tuple[str, ...]


def _months_in(since: date, until: date) -> list[date]:
    months = []
    cursor = date(since.year, since.month, 1)
    end = date(until.year, until.month, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return months


def _cn_entities(conn: psycopg.Connection) -> list[_CnEntity]:
    rows = conn.execute(
        "SELECT entity_id, tushare_code, name_short, name_full FROM core.entity"
        " WHERE market = ANY(%s) AND tushare_code IS NOT NULL",
        (list(_CN_MARKETS),),
    ).fetchall()
    return [
        _CnEntity(
            entity_id=r[0],
            tushare_code=r[1],
            short_names=tuple(n for n in (r[2], r[3]) if n),
        )
        for r in rows
    ]


def _has_matching_cninfo_row(
    conn: psycopg.Connection, entity_id: str, title_norm: str, day: date
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM core.announcement_index"
        " WHERE entity_id = %s AND source = 'cninfo' AND superseded_at IS NULL"
        "   AND title_norm = %s AND publish_date BETWEEN %s AND %s",
        (entity_id, title_norm, day - timedelta(days=1), day + timedelta(days=1)),
    ).fetchone()
    return row is not None


def check_announcement_coverage(
    conn: psycopg.Connection,
    *,
    wind: WindDocsAdapter,
    blob: BlobStore,
    since: date,
    until: date,
    partition_date: date,
    ingest_run_id: str,
) -> list[CoverageGap]:
    gaps: list[CoverageGap] = []
    for entity in _cn_entities(conn):
        for month in _months_in(since, until):
            ctx = FetchContext(ingest_run_id=ingest_run_id, partition_date=partition_date)
            raw = wind.fetch_month(ctx, short_name=entity.short_names[0], month=month)
            record_snapshot(conn, raw, ingest_run_id=ingest_run_id, blob=blob)
            entries = wind.parse_entries(
                raw,
                entity_ref=entity.tushare_code,
                short_names=entity.short_names,
                since=since,
                until=until,
            )
            for entry in entries:
                title_norm = normalize_title(entry.title, list(entity.short_names))
                if not _has_matching_cninfo_row(
                    conn, entity.entity_id, title_norm, entry.publish_date
                ):
                    gaps.append(CoverageGap(entity.tushare_code, entry.title, entry.publish_date))

    note = (
        "; ".join(f"{g.entity_ref}:{g.publish_date}:{g.title}" for g in gaps[:20]) if gaps else None
    )
    record_metric(
        conn,
        partition_date,
        MetricResult(
            metric=METRIC_NAME,
            value=float(len(gaps)),
            passed=len(gaps) == 0,
            threshold=0.0,
            source_id="wind",
            note=note,
        ),
    )
    return gaps
