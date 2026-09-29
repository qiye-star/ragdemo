"""公告对账（"公告不遗漏"的验收依据）：用来源**自己报的总数**与库里现行行数比对。

不信入库时那一次运行的自检，而是验收时重新向来源要一遍：
- 巨潮：`totalAnnouncement` 与实际列出的条数（`collect_refs` 已做同一对账），再与库里
  `source='cninfo'` 的现行行数比；
- 披露易：各完整子窗口 `recordCnt` 之和，同样再与库比；
- EDGAR：来源没有总数，用 recent + 分页文件列出的跟踪表单数与库比，并在窗口起点早于
  能取到的最早一条时给出提示（来源可能根本没有那么早的数据，也可能是没取全）。

降级来源（iFind）只是片段，不做对账——A 股以巨潮为准。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import psycopg

from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.errors import AdapterError
from ragdemo.ingest.announcements import AnnouncementSources, EntityRoute, market_group
from ragdemo.ingest.cninfo_docs import collect_refs
from ragdemo.ingest.hkexnews_docs import collect_hk_refs


@dataclass(frozen=True)
class VerifyRow:
    entity_ref: str
    group: str
    source_total: int | None  # 来源自报（或列出）的条数；取不到为 None
    db_count: int
    ok: bool
    note: str = ""


def _db_count(
    conn: psycopg.Connection, *, entity_ref: str, source: str, since: date, until: date
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM core.announcement_index a JOIN core.entity e USING (entity_id)"
        " WHERE (e.tushare_code = %s OR e.ifind_code = %s OR e.wind_code = %s OR e.edgar_cik = %s)"
        " AND a.source = %s AND a.superseded_at IS NULL AND a.publish_date BETWEEN %s AND %s",
        (entity_ref, entity_ref, entity_ref, entity_ref, source, since, until),
    ).fetchone()
    assert row is not None
    return int(row[0])


def verify_entity(
    conn: psycopg.Connection,
    route: EntityRoute,
    sources: AnnouncementSources,
    *,
    since: date,
    until: date,
) -> VerifyRow | None:
    """返回 `None` 表示该实体不在对账范围（市场不支持 / 没有对应来源配置）。"""
    group = market_group(route)
    ctx = FetchContext(ingest_run_id="verify", partition_date=until)
    since_dt = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
    until_dt = datetime.combine(until, datetime.min.time(), tzinfo=UTC)
    try:
        if group == "cn" and sources.cninfo is not None:
            code = route.entity_ref.split(".")[0]
            adapter = sources.cninfo.adapter
            raws = list(adapter.fetch(ctx, stock_code=code, since=since_dt, until=until_dt))
            refs = collect_refs(adapter, raws, stock_code=code)
            total: int | None = adapter.expected_total(raws)
            source = "cninfo"
            listed = len(refs)
        elif group == "hk" and sources.hkexnews is not None:
            code = route.entity_ref.split(".")[0].zfill(5)
            hk = sources.hkexnews.adapter
            raws = list(hk.fetch(ctx, stock_code=code, since=since_dt, until=until_dt))
            hk_refs = collect_hk_refs(hk, raws, stock_code=code)
            total = hk.expected_total(raws)
            source = "hkexnews"
            listed = len(hk_refs)
        elif group == "us" and sources.edgar is not None and route.cik is not None:
            ed = sources.edgar.adapter
            filings = {
                f.accession: f
                for raw in ed.fetch(ctx, cik=route.cik, since=since)
                for f in ed.parse_filings(raw)
                if since <= f.acceptance_datetime.date() <= until
            }
            total = listed = len(filings)
            source = "edgar"
        else:
            return None
    except AdapterError as exc:
        return VerifyRow(route.entity_ref, group or "", None, 0, False, f"目录不完整：{exc}")

    db = _db_count(conn, entity_ref=route.entity_ref, source=source, since=since, until=until)
    if total is None:
        total = listed
    ok = db == total == listed
    note = "" if ok else f"来源 {total}（列出 {listed}）≠ 库 {db}"
    return VerifyRow(route.entity_ref, group or "", total, db, ok, note)


def verify_announcements(
    conn: psycopg.Connection,
    routes: list[EntityRoute],
    sources: AnnouncementSources,
    *,
    since: date,
    until: date,
) -> list[VerifyRow]:
    rows = [verify_entity(conn, r, sources, since=since, until=until) for r in routes]
    return [r for r in rows if r is not None]
