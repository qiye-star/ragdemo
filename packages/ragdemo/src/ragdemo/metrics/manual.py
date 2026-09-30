"""手工录入的产业指标（第三方行业报告、法说会等没有供应商接口的数据）。

CLAUDE.md §0：每个数字必须可溯源。手工录入不是例外——每一行必须写明**来源**（哪份报告）
和**出处**（页码 / 链接），`known_at` 取报告的发布日（不是录入日）。整个 CSV 文件的原样内容
作为一条 `provider='manual'` 的 `provider_snapshot` 留底，每个事实行都盖上它的 snapshot_id。

校验在写库之前一次报全，任何一行不过就整批不写：
- 指标必须存在，且不是 `derived`（推导指标由 Python 计算，不接受手工值）；
- 单位必须等于指标定义里的单位（不在这里换算，避免静默错单位）；
- 公司口径填 `entity_id`（必须存在，写 `core.fin_fact`）；行业汇总口径 `entity_id` 留空、
  填 `scope`（GLOBAL / CN 等，写 `core.industry_metric_point`），两者恰填其一；
- `known_at`（发布日）不得是未来，报告期末不得晚于发布日；
- 来源、出处不得为空。
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

import psycopg

from ragdemo.adapters.base import FactRecord, RawResponse
from ragdemo.ingest.snapshot import save_snapshot
from ragdemo.ingest.writer import OutOfOrderCorrection, PointInTimeWriter, WriteOutcome
from ragdemo.metrics.definitions import SourceMapSpec, publish_source_map

PROVIDER = "manual"
COLUMNS = (
    "metric_id",
    "entity_id",
    "period",
    "period_end",
    "value",
    "unit",
    "published_on",
    "source",
    "source_ref",
    "scope",
)
_BEIJING = timezone(timedelta(hours=8))
_SCOPE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class ManualImportError(ValueError):
    """校验不过；消息里包含全部问题（一次报全）。"""


@dataclass(frozen=True)
class ManualRow:
    line: int
    metric_id: str
    entity_id: str
    period: str
    period_end: date
    value: Decimal
    unit: str
    published_on: date
    source: str
    source_ref: str
    scope: str = ""

    @property
    def known_at(self) -> datetime:
        """日精度来源的保守上界：发布当日 23:59:59.999999 北京时间。"""
        return datetime.combine(self.published_on, time(23, 59, 59, 999999), tzinfo=_BEIJING)


def write_template(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerow(COLUMNS)


def _date(raw: str, name: str, line: int, problems: list[str]) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        problems.append(f"第 {line} 行 {name}={raw!r} 不是 YYYY-MM-DD")
        return None


def parse_rows(
    conn: psycopg.Connection, path: Path, *, today: date | None = None
) -> list[ManualRow]:
    today = today or datetime.now(UTC).astimezone(_BEIJING).date()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS[:-1] if c not in (reader.fieldnames or [])]
        if missing:
            raise ManualImportError(f"缺少列：{', '.join(missing)}（表头应为 {','.join(COLUMNS)}）")
        raw_rows = [
            {"scope": "", **{k: (v or "").strip() for k, v in r.items() if k}} for r in reader
        ]

    metrics = {
        str(r[0]): (str(r[1]), str(r[2]))
        for r in conn.execute("SELECT metric_id, unit, source_type FROM core.node_metric")
    }
    entities = {str(r[0]) for r in conn.execute("SELECT entity_id FROM core.entity")}

    problems: list[str] = []
    rows: list[ManualRow] = []
    seen: set[tuple[str, str, str]] = set()
    for i, r in enumerate(raw_rows, start=2):
        before = len(problems)
        metric = metrics.get(r["metric_id"])
        if metric is None:
            problems.append(f"第 {i} 行 metric_id {r['metric_id']!r} 不存在")
        elif metric[1] == "derived":
            problems.append(f"第 {i} 行 {r['metric_id']} 是推导指标，由代码计算，不接受手工值")
        elif r["unit"] != metric[0]:
            problems.append(
                f"第 {i} 行 {r['metric_id']} 单位应为 {metric[0]!r}，填的是 {r['unit']!r}"
                "（不在导入时换算，请在表里换好）"
            )
        if bool(r["entity_id"]) == bool(r["scope"]):
            problems.append(f"第 {i} 行 entity_id 与 scope 必须恰填其一（公司口径 / 行业汇总口径）")
        elif r["entity_id"] and r["entity_id"] not in entities:
            problems.append(f"第 {i} 行 entity_id {r['entity_id']!r} 不存在")
        elif r["scope"] and not _SCOPE.match(r["scope"]):
            problems.append(f"第 {i} 行 scope {r['scope']!r} 只能含字母数字和 _.-（≤32 字符）")
        for name in ("period", "source", "source_ref"):
            if not r[name]:
                problems.append(f"第 {i} 行 {name} 不能为空（每个数字必须可溯源）")
        try:
            value = Decimal(r["value"])
            if not value.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            problems.append(f"第 {i} 行 value={r['value']!r} 不是数字")
            value = Decimal(0)
        period_end = _date(r["period_end"], "period_end", i, problems)
        published = _date(r["published_on"], "published_on", i, problems)
        if published is not None and published > today:
            problems.append(f"第 {i} 行 published_on {published} 是未来日期")
        if period_end and published and period_end > published:
            problems.append(f"第 {i} 行报告期末 {period_end} 晚于发布日 {published}")
        key = (r["metric_id"], r["entity_id"] or f"scope:{r['scope']}", r["period"])
        if key in seen:
            problems.append(f"第 {i} 行与前面重复：{key}")
        seen.add(key)
        if len(problems) == before and period_end and published:
            rows.append(
                ManualRow(
                    i,
                    r["metric_id"],
                    r["entity_id"],
                    r["period"],
                    period_end,
                    value,
                    r["unit"],
                    published,
                    r["source"],
                    r["source_ref"],
                    r["scope"],
                )
            )
    if problems:
        raise ManualImportError("\n".join(problems))
    return rows


def ensure_source_maps(
    conn: psycopg.Connection, metric_ids: Iterable[str], *, now: datetime
) -> None:
    """登记 manual 的字段映射（字段名 = metric_id，不换算）。已登记的不动。"""
    for metric_id in sorted(set(metric_ids)):
        publish_source_map(
            conn,
            SourceMapSpec(metric_id, PROVIDER, metric_id, priority=200, notes="手工录入"),
            author="manual-import",
            known_at=now,
            ingest_run_id="manual-import",
        )


def import_rows(
    conn: psycopg.Connection, path: Path, rows: list[ManualRow], *, now: datetime
) -> dict[WriteOutcome, int]:
    run_id = f"manual:{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}"
    ensure_source_maps(conn, (r.metric_id for r in rows), now=now)
    snapshot_id = save_snapshot(
        conn,
        RawResponse(
            provider=PROVIDER,
            endpoint=path.name,
            params={"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            payload={
                "columns": list(COLUMNS),
                "csv": path.read_text(encoding="utf-8-sig"),
            },
            http_status=200,
            fetched_at=now,
        ),
        ingest_run_id=run_id,
    )
    writer = PointInTimeWriter(conn, ingest_run_id=run_id, source=PROVIDER)
    counts: dict[WriteOutcome, int] = {}
    for r in rows:
        if not r.entity_id:
            outcome = _write_industry_point(conn, r, snapshot_id, run_id)
            counts[outcome] = counts.get(outcome, 0) + 1
            continue
        outcome = writer.write_fact(
            FactRecord(
                entity_ref=r.entity_id,
                metric_field=r.metric_id,
                period=r.period,
                period_end=r.period_end,
                value=float(r.value),
                unit=r.unit,
                currency=None,
                valid_from=r.period_end,
                known_at=r.known_at,
                source_ref=f"{r.source} | {r.source_ref}",
                snapshot_id=snapshot_id,
            )
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    conn.commit()
    return counts


def _write_industry_point(
    conn: psycopg.Connection, r: ManualRow, snapshot_id: int, run_id: str
) -> WriteOutcome:
    """行业汇总口径写 core.industry_metric_point；规则与 write_fact 相同：
    同值跳过，值变了追加更正（旧行 superseded_at = 新行 known_at），新 known_at 必须更晚。"""
    with conn.transaction():
        live = conn.execute(
            "SELECT value, known_at FROM core.industry_metric_point"
            " WHERE metric_id = %s AND scope = %s AND period = %s AND superseded_at IS NULL"
            " FOR UPDATE",
            (r.metric_id, r.scope, r.period),
        ).fetchone()
        if live is not None:
            if abs(float(cast(Decimal, live[0])) - float(r.value)) < 1e-9:
                return WriteOutcome.SKIPPED_IDENTICAL
            if r.known_at <= cast(datetime, live[1]):
                raise OutOfOrderCorrection(
                    f"{r.metric_id}/{r.scope}/{r.period}: 新发布日 {r.published_on} "
                    f"不晚于现有 {live[1]}"
                )
            conn.execute(
                "UPDATE core.industry_metric_point SET superseded_at = %s WHERE metric_id = %s"
                " AND scope = %s AND period = %s AND superseded_at IS NULL",
                (r.known_at, r.metric_id, r.scope, r.period),
            )
            outcome = WriteOutcome.CORRECTED
        else:
            outcome = WriteOutcome.INSERTED
        conn.execute(
            "INSERT INTO core.industry_metric_point (metric_id, scope, period, period_end, value,"
            " unit, valid_from, known_at, source, source_ref, snapshot_id, ingest_run_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                r.metric_id,
                r.scope,
                r.period,
                r.period_end,
                r.value,
                r.unit,
                r.period_end,
                r.known_at,
                r.source,
                r.source_ref,
                snapshot_id,
                run_id,
            ),
        )
    return outcome
