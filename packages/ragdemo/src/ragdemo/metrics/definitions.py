"""口径的版本化写入与时点读取（总计划阶段 1 任务 1.2，迁移 018）。

三类口径：指标定义（node_metric）、供应商字段映射与换算系数（metric_source_map）、
AI 收入拆分规则（ai_revenue_rule）。写入一律"追加新版本 + 旧版本 superseded"，
从不覆盖；读取一律经 asof 视图，按 as_of 看到当时生效的版本。

`publish_*` 不提交事务——调用方（如 Excel 导入）把一批修改放在一个事务里，
全部通过才提交，中途任何一条失败整批回滚。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Generic, TypeVar

import psycopg
from psycopg.rows import dict_row

from ragdemo_core.db.session import as_of_session

# 迁移 018 给存量口径生成版本 1 时用的同一个时刻（有测试守着两者一致）。
METRIC_BASELINE_KNOWN_AT = datetime(2000, 1, 1, tzinfo=timezone(timedelta(hours=8)))


class OutOfOrderVersion(ValueError):
    """新版本的 known_at 不晚于当前活版本——时间线不能倒着写。"""


class PublishOutcome(enum.Enum):
    CREATED = "created"  # 第一次出现
    NEW_VERSION = "new_version"  # 内容变了，追加一个版本
    UNCHANGED = "unchanged"  # 内容与活版本完全相同，什么都不写


@dataclass(frozen=True)
class PublishResult:
    outcome: PublishOutcome
    version: int


@dataclass(frozen=True)
class MetricSpec:
    """指标定义中可编辑的口径字段（metric_id 是身份，不可改）。"""

    metric_id: str
    metric_name: str
    metric_role: str  # leading / confirming
    frequency: str
    source_type: str
    definition: str
    unit: str
    l3_node: str | None = None
    source_entity: tuple[str, ...] = ()
    extraction_hint: str | None = None
    direction: int = 1


@dataclass(frozen=True)
class SourceMapSpec:
    metric_id: str
    provider: str
    provider_field: str
    priority: int = 100
    scale_factor: Decimal = Decimal(1)
    notes: str | None = None


@dataclass(frozen=True)
class AiRevenueRuleSpec:
    entity_id: str
    rule_text: str
    source_type: str  # segment_report / management_guidance / analyst / estimate
    confidence: str  # core.confidence 枚举
    effective_from: date


S = TypeVar("S")


@dataclass(frozen=True)
class Versioned(Generic[S]):
    """某个 as_of 下生效的一版口径。`version` 对指标与字段映射是该键的第几版；
    对 AI 收入拆分规则是规则行的 rule_id（单调递增，但同一实体的版本不一定连续）。"""

    spec: S
    version: int
    author: str
    change_note: str | None
    known_at: datetime


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区，收到 naive datetime: {value!r}")


def _normalize(value: Any) -> Any:  # noqa: ANN401
    """把数据库读回的值与 spec 里的值放到同一种形态下比较。"""
    if isinstance(value, list | tuple):
        return tuple(value)
    if isinstance(value, Decimal | int) and not isinstance(value, bool):
        return Decimal(value)
    return value


def _same(spec: Any, row: dict[str, Any]) -> bool:  # noqa: ANN401
    return all(_normalize(getattr(spec, f.name)) == _normalize(row[f.name]) for f in fields(spec))


def _check_order(live_known_at: datetime, known_at: datetime, what: str) -> None:
    if known_at <= live_known_at:
        raise OutOfOrderVersion(
            f"{what}: 新版本 known_at {known_at.isoformat()} 不晚于当前版本 "
            f"{live_known_at.isoformat()}——口径版本只能按时间顺序追加"
        )


# --- 指标定义 --------------------------------------------------------------------

_METRIC_COLS = [f.name for f in fields(MetricSpec)]
_METRIC_EDITABLE = [c for c in _METRIC_COLS if c != "metric_id"]


def publish_metric(
    conn: psycopg.Connection,
    spec: MetricSpec,
    *,
    author: str,
    known_at: datetime,
    ingest_run_id: str,
    change_note: str | None = None,
    source: str | None = None,
) -> PublishResult:
    _require_aware(known_at, "known_at")
    values = {c: getattr(spec, c) for c in _METRIC_COLS}
    values["source_entity"] = list(spec.source_entity)
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        live = cur.execute(
            f"SELECT version, known_at, {', '.join(_METRIC_COLS)}"
            "  FROM core.node_metric_version"
            " WHERE metric_id = %s AND superseded_at IS NULL FOR UPDATE",
            (spec.metric_id,),
        ).fetchone()
        if live is not None and _same(spec, live):
            return PublishResult(PublishOutcome.UNCHANGED, int(live["version"]))
        if live is not None:
            _check_order(live["known_at"], known_at, spec.metric_id)
            conn.execute(
                "UPDATE core.node_metric_version SET superseded_at = %s"
                " WHERE metric_id = %s AND superseded_at IS NULL",
                (known_at, spec.metric_id),
            )
        version = 1 if live is None else int(live["version"]) + 1

        exists = conn.execute(
            "SELECT 1 FROM core.node_metric WHERE metric_id = %s", (spec.metric_id,)
        ).fetchone()
        if exists is None:
            conn.execute(
                f"INSERT INTO core.node_metric ({', '.join(_METRIC_COLS)})"
                f" VALUES ({', '.join(f'%({c})s' for c in _METRIC_COLS)})",
                values,
            )
        else:
            assignments = ", ".join(f"{c} = %({c})s" for c in _METRIC_EDITABLE)
            conn.execute(
                f"UPDATE core.node_metric SET {assignments} WHERE metric_id = %(metric_id)s",
                values,
            )
        conn.execute(
            f"INSERT INTO core.node_metric_version ({', '.join(_METRIC_COLS)}, version, author,"
            " change_note, valid_from, known_at, source, ingest_run_id)"
            f" VALUES ({', '.join(f'%({c})s' for c in _METRIC_COLS)}, %(version)s, %(author)s,"
            " %(change_note)s, %(valid_from)s, %(known_at)s, %(source)s, %(ingest_run_id)s)",
            {
                **values,
                "version": version,
                "author": author,
                "change_note": change_note,
                "valid_from": known_at.date(),
                "known_at": known_at,
                "source": source or f"manual:{author}",
                "ingest_run_id": ingest_run_id,
            },
        )
    outcome = PublishOutcome.CREATED if live is None else PublishOutcome.NEW_VERSION
    return PublishResult(outcome, version)


def metric_as_of(
    conn: psycopg.Connection, metric_id: str, *, as_of: datetime
) -> Versioned[MetricSpec] | None:
    with as_of_session(conn, as_of) as session:
        row = (
            session.cursor(row_factory=dict_row)
            .execute(
                f"SELECT {', '.join(_METRIC_COLS)}, version, author, change_note, known_at"
                "  FROM asof.node_metric_version WHERE metric_id = %s",
                (metric_id,),
            )
            .fetchone()
        )
    if row is None:
        return None
    spec = MetricSpec(
        **{c: row[c] for c in _METRIC_COLS if c != "source_entity"},
        source_entity=tuple(row["source_entity"]),
    )
    return Versioned(spec, int(row["version"]), row["author"], row["change_note"], row["known_at"])


# --- 供应商字段映射 --------------------------------------------------------------

_MAP_KEY = ["metric_id", "provider", "provider_field"]
_MAP_COLS = [f.name for f in fields(SourceMapSpec)]
_MAP_EDITABLE = [c for c in _MAP_COLS if c not in _MAP_KEY]


def publish_source_map(
    conn: psycopg.Connection,
    spec: SourceMapSpec,
    *,
    author: str,
    known_at: datetime,
    ingest_run_id: str,
    change_note: str | None = None,
    source: str | None = None,
) -> PublishResult:
    _require_aware(known_at, "known_at")
    values = {c: getattr(spec, c) for c in _MAP_COLS}
    key_sql = " AND ".join(f"{c} = %({c})s" for c in _MAP_KEY)
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        live = cur.execute(
            f"SELECT version, known_at, {', '.join(_MAP_COLS)}"
            f"  FROM core.metric_source_map_version WHERE {key_sql} AND superseded_at IS NULL"
            " FOR UPDATE",
            values,
        ).fetchone()
        if live is not None and _same(spec, live):
            return PublishResult(PublishOutcome.UNCHANGED, int(live["version"]))
        what = f"{spec.metric_id}/{spec.provider}/{spec.provider_field}"
        if live is not None:
            _check_order(live["known_at"], known_at, what)
            conn.execute(
                "UPDATE core.metric_source_map_version SET superseded_at = %(known_at)s"
                f" WHERE {key_sql} AND superseded_at IS NULL",
                {**values, "known_at": known_at},
            )
        version = 1 if live is None else int(live["version"]) + 1

        conn.execute(
            f"INSERT INTO core.metric_source_map ({', '.join(_MAP_COLS)})"
            f" VALUES ({', '.join(f'%({c})s' for c in _MAP_COLS)})"
            f" ON CONFLICT ({', '.join(_MAP_KEY)}) DO UPDATE SET"
            f" {', '.join(f'{c} = EXCLUDED.{c}' for c in _MAP_EDITABLE)}",
            values,
        )
        conn.execute(
            f"INSERT INTO core.metric_source_map_version ({', '.join(_MAP_COLS)}, version,"
            " author, change_note, valid_from, known_at, source, ingest_run_id)"
            f" VALUES ({', '.join(f'%({c})s' for c in _MAP_COLS)}, %(version)s, %(author)s,"
            " %(change_note)s, %(valid_from)s, %(known_at)s, %(source)s, %(ingest_run_id)s)",
            {
                **values,
                "version": version,
                "author": author,
                "change_note": change_note,
                "valid_from": known_at.date(),
                "known_at": known_at,
                "source": source or f"manual:{author}",
                "ingest_run_id": ingest_run_id,
            },
        )
    outcome = PublishOutcome.CREATED if live is None else PublishOutcome.NEW_VERSION
    return PublishResult(outcome, version)


def source_maps_as_of(
    conn: psycopg.Connection, metric_id: str, *, as_of: datetime
) -> list[Versioned[SourceMapSpec]]:
    with as_of_session(conn, as_of) as session:
        rows = (
            session.cursor(row_factory=dict_row)
            .execute(
                f"SELECT {', '.join(_MAP_COLS)}, version, author, change_note, known_at"
                "  FROM asof.metric_source_map_version WHERE metric_id = %s"
                " ORDER BY priority, provider, provider_field",
                (metric_id,),
            )
            .fetchall()
        )
    return [
        Versioned(
            SourceMapSpec(**{c: r[c] for c in _MAP_COLS}),
            int(r["version"]),
            r["author"],
            r["change_note"],
            r["known_at"],
        )
        for r in rows
    ]


# --- AI 收入拆分规则 -------------------------------------------------------------

_RULE_COLS = [f.name for f in fields(AiRevenueRuleSpec)]


def publish_ai_revenue_rule(
    conn: psycopg.Connection,
    spec: AiRevenueRuleSpec,
    *,
    author: str,
    known_at: datetime,
    ingest_run_id: str,
    change_note: str | None = None,
) -> PublishResult:
    """每个实体同一时刻只有一条活规则。返回的 version 是新规则行的 rule_id。"""
    _require_aware(known_at, "known_at")
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        live = cur.execute(
            f"SELECT rule_id, known_at, {', '.join(_RULE_COLS)} FROM core.ai_revenue_rule"
            " WHERE entity_id = %s AND superseded_at IS NULL FOR UPDATE",
            (spec.entity_id,),
        ).fetchone()
        if live is not None and _same(spec, live):
            return PublishResult(PublishOutcome.UNCHANGED, int(live["rule_id"]))
        if live is not None:
            _check_order(live["known_at"], known_at, f"ai_revenue_rule/{spec.entity_id}")
            conn.execute(
                "UPDATE core.ai_revenue_rule SET superseded_at = %s"
                " WHERE entity_id = %s AND superseded_at IS NULL",
                (known_at, spec.entity_id),
            )
        inserted = conn.execute(
            "INSERT INTO core.ai_revenue_rule (entity_id, rule_text, source_type, confidence,"
            " author, effective_from, valid_from, known_at, source, ingest_run_id, change_note)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING rule_id",
            (
                spec.entity_id,
                spec.rule_text,
                spec.source_type,
                spec.confidence,
                author,
                spec.effective_from,
                spec.effective_from,
                known_at,
                f"manual:{author}",
                ingest_run_id,
                change_note,
            ),
        ).fetchone()
        assert inserted is not None
    outcome = PublishOutcome.CREATED if live is None else PublishOutcome.NEW_VERSION
    return PublishResult(outcome, int(inserted[0]))


def ai_revenue_rule_as_of(
    conn: psycopg.Connection, entity_id: str, *, as_of: datetime
) -> Versioned[AiRevenueRuleSpec] | None:
    with as_of_session(conn, as_of) as session:
        row = (
            session.cursor(row_factory=dict_row)
            .execute(
                f"SELECT {', '.join(_RULE_COLS)}, rule_id, author, change_note, known_at"
                "  FROM asof.ai_revenue_rule WHERE entity_id = %s",
                (entity_id,),
            )
            .fetchone()
        )
    if row is None:
        return None
    spec = AiRevenueRuleSpec(**{c: row[c] for c in _RULE_COLS})
    return Versioned(spec, int(row["rule_id"]), row["author"], row["change_note"], row["known_at"])
