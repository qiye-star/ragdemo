"""口径 Excel 导入：校验 → 差异预览 → 按版本追加写入（总计划阶段 1 任务 1.3）。

`plan_import` 只读：把模板内容与当前生效的口径逐行对比，得出每一行是新增、新版本
还是不变，以及具体哪些字段从什么改成了什么。任何一条校验不过就抛 `WorkbookError`，
一次报全，不写库。

`apply_import` 把计划里有变化的行放进**一个事务**依次发布：指标定义先于字段映射
（外键），任一行被拒（典型是时间线乱序）整批回滚，库里不留半截修改。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

import psycopg

from ragdemo.metrics.definitions import (
    AiRevenueRuleSpec,
    MetricSpec,
    OutOfOrderVersion,
    PublishOutcome,
    SourceMapSpec,
    current_ai_revenue_rules,
    current_metrics,
    current_source_maps,
    publish_ai_revenue_rule,
    publish_metric,
    publish_source_map,
)
from ragdemo.metrics.workbook import (
    METRIC_SHEET,
    RULE_SHEET,
    SOURCE_MAP_SHEET,
    WorkbookData,
    WorkbookError,
)

INGEST_RUN_ID = "cli:metrics-import"


class ImportRejected(RuntimeError):
    """写入时被拒（如某一行的新版本早于库里已有的版本），整批已回滚。"""


@dataclass(frozen=True)
class Change:
    sheet: str
    key: str
    outcome: PublishOutcome
    diffs: list[tuple[str, object, object]]  # (字段, 修改前, 修改后)
    spec: MetricSpec | SourceMapSpec | AiRevenueRuleSpec


@dataclass(frozen=True)
class ImportPlan:
    changes: list[Change]

    def changed(self) -> list[Change]:
        return [c for c in self.changes if c.outcome is not PublishOutcome.UNCHANGED]


def _plain(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _diff(before: Any | None, after: Any) -> list[tuple[str, object, object]]:  # noqa: ANN401
    diffs: list[tuple[str, object, object]] = []
    for f in fields(after):
        new = getattr(after, f.name)
        old = None if before is None else getattr(before, f.name)
        if before is None:
            if new not in (None, (), ""):
                diffs.append((f.name, None, new))
        elif _plain(old) != _plain(new):
            diffs.append((f.name, old, new))
    return diffs


def _change(sheet: str, key: str, before: Any | None, after: Any) -> Change:  # noqa: ANN401
    diffs = _diff(before, after)
    if before is None:
        outcome = PublishOutcome.CREATED
    elif diffs:
        outcome = PublishOutcome.NEW_VERSION
    else:
        outcome = PublishOutcome.UNCHANGED
    return Change(sheet, key, outcome, diffs, after)


def _duplicates(keys: list[tuple[int, str]], sheet: str) -> list[str]:
    seen: dict[str, int] = {}
    problems: list[str] = []
    for number, key in keys:
        if key in seen:
            problems.append(f"{sheet} 第 {number} 行: {key} 与第 {seen[key]} 行重复")
        else:
            seen[key] = number
    return problems


def plan_import(conn: psycopg.Connection, data: WorkbookData) -> ImportPlan:
    problems = list(data.problems)  # 逐行解析问题（含环节名校验）已在 read_workbook 里收集
    problems += _duplicates([(r.row_number, r.spec.metric_id) for r in data.metrics], METRIC_SHEET)
    problems += _duplicates(
        [
            (r.row_number, f"{r.spec.metric_id}/{r.spec.provider}/{r.spec.provider_field}")
            for r in data.source_maps
        ],
        SOURCE_MAP_SHEET,
    )
    problems += _duplicates([(r.row_number, r.spec.entity_id) for r in data.rules], RULE_SHEET)

    live_metrics = current_metrics(conn)
    known_metric_ids = set(live_metrics) | {r.spec.metric_id for r in data.metrics}
    for map_row in data.source_maps:
        if map_row.spec.metric_id not in known_metric_ids:
            problems.append(
                f"{map_row.sheet} 第 {map_row.row_number} 行: metric_id"
                f"「{map_row.spec.metric_id}」既不在库里，也不在本文件的「指标定义」表里"
            )
    entity_ids = {str(r[0]) for r in conn.execute("SELECT entity_id FROM core.entity").fetchall()}
    for rule_row in data.rules:
        if rule_row.spec.entity_id not in entity_ids:
            problems.append(
                f"{rule_row.sheet} 第 {rule_row.row_number} 行: entity_id"
                f"「{rule_row.spec.entity_id}」不在 core.entity 里"
            )
    if problems:
        raise WorkbookError(problems)

    live_maps = current_source_maps(conn)
    live_rules = current_ai_revenue_rules(conn)
    changes = [
        _change(METRIC_SHEET, r.spec.metric_id, live_metrics.get(r.spec.metric_id), r.spec)
        for r in data.metrics
    ]
    changes += [
        _change(
            SOURCE_MAP_SHEET,
            f"{r.spec.metric_id}/{r.spec.provider}/{r.spec.provider_field}",
            live_maps.get((r.spec.metric_id, r.spec.provider, r.spec.provider_field)),
            r.spec,
        )
        for r in data.source_maps
    ]
    changes += [
        _change(RULE_SHEET, r.spec.entity_id, live_rules.get(r.spec.entity_id), r.spec)
        for r in data.rules
    ]
    return ImportPlan(changes)


def apply_import(
    conn: psycopg.Connection,
    plan: ImportPlan,
    *,
    author: str,
    change_note: str | None,
    known_at: datetime,
) -> Counter[PublishOutcome]:
    """一个事务发布全部变化；返回按结果分类的计数。任一行被拒则整批回滚。"""
    counts: Counter[PublishOutcome] = Counter()
    order = {METRIC_SHEET: 0, SOURCE_MAP_SHEET: 1, RULE_SHEET: 2}
    common: dict[str, Any] = {
        "author": author,
        "known_at": known_at,
        "change_note": change_note,
        "ingest_run_id": INGEST_RUN_ID,
    }
    try:
        with conn.transaction():
            for change in sorted(plan.changed(), key=lambda c: order[c.sheet]):
                spec = change.spec
                if isinstance(spec, MetricSpec):
                    result = publish_metric(conn, spec, **common)
                elif isinstance(spec, SourceMapSpec):
                    result = publish_source_map(conn, spec, **common)
                else:
                    result = publish_ai_revenue_rule(conn, spec, **common)
                counts[result.outcome] += 1
    except OutOfOrderVersion as exc:
        conn.rollback()
        raise ImportRejected(f"整批已回滚，没有写入任何修改：{exc}") from exc
    conn.commit()
    counts[PublishOutcome.UNCHANGED] += len(plan.changes) - len(plan.changed())
    return counts
