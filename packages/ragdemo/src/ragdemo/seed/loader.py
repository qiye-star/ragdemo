"""种子 CSV 导入：先全量校验，再整体写入。

「先校验后写入」而不是边读边写：一份 CSV 里有一行错就整份拒绝。
部分导入的种子数据比没有种子数据更糟——它看起来成功了，实际缺了一批实体，
而下游的关系导入会因为外键失败得莫名其妙。
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import psycopg

from ragdemo.seed.taxonomy import DEFAULT_TAXONOMY_PATH, Taxonomy, load_taxonomy

ENTITY_ID_RE = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9._-]{1,32}$")
# 手工数据统一按 UTC 当日 00:00 记，见 docs/03-point-in-time.md §6.3
SEED_TZ = UTC
SEED_RUN_ID = "seed"
SEED_SOURCE = "manual:seed"

Connection = psycopg.Connection[tuple[object, ...]]


class SeedError(ValueError):
    """种子数据校验失败。"""


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _split_nodes(raw: str) -> list[str]:
    return [n for n in (s.strip() for s in raw.split("|")) if n]


def _known_at_for(valid_from: date) -> datetime:
    """手工数据的 known_at = valid_from 当日 00:00，不是 now()。"""
    return datetime.combine(valid_from, time.min, tzinfo=SEED_TZ)


def _taxonomy_or_default(taxonomy: Taxonomy | None) -> Taxonomy:
    return taxonomy if taxonomy is not None else load_taxonomy(DEFAULT_TAXONOMY_PATH)


def _validate_entities(rows: list[dict[str, str]], taxonomy: Taxonomy) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=2):  # 第 1 行是表头
        eid = row["entity_id"]
        if not ENTITY_ID_RE.match(eid):
            raise SeedError(f"第 {i} 行 entity_id 格式非法: {eid!r}（应形如 CN.688256）")
        if eid in seen:
            raise SeedError(f"第 {i} 行 entity_id 重复: {eid}")
        seen.add(eid)

        nodes = _split_nodes(row["l3_node"])
        if not nodes:
            raise SeedError(f"第 {i} 行 l3_node 为空: {eid}")
        if row["primary_node"] not in nodes:
            raise SeedError(
                f"第 {i} 行 primary_node {row['primary_node']!r} 不在 l3_node {nodes} 中: {eid}"
            )
        unknown = taxonomy.unknown([*nodes, row["primary_node"]])
        if unknown:
            raise SeedError(f"第 {i} 行环节名不在环节表中: {unknown}（{eid}）")
        if not row["listed_date"]:
            raise SeedError(f"第 {i} 行缺少 listed_date: {eid}（观点评分需要它剔除次新股）")

        out.append(
            {
                **row,
                "l3_node": nodes,
                "listed_date": date.fromisoformat(row["listed_date"]),
                "fiscal_year_end": int(row["fiscal_year_end"]) if row["fiscal_year_end"] else None,
            }
        )
    return out


def load_entities(conn: Connection, csv_path: Path, taxonomy: Taxonomy | None = None) -> int:
    """导入实体，同时为每个 l3_node 写一行 entity_node_membership。"""
    records = _validate_entities(_read(csv_path), _taxonomy_or_default(taxonomy))
    with conn.transaction():
        for r in records:
            conn.execute(
                "INSERT INTO core.entity (entity_id, name_full, name_short, name_en,"
                " entity_type, market, tushare_code, l1_layer, l2_segment, l3_node,"
                " primary_node, hq_country, status, listed_date, currency,"
                " fiscal_year_end, notes) "
                "VALUES (%(entity_id)s,%(name_full)s,%(name_short)s,%(name_en)s,"
                " %(entity_type)s,%(market)s,%(tushare_code)s,%(l1_layer)s,%(l2_segment)s,"
                " %(l3_node)s,%(primary_node)s,%(hq_country)s,%(status)s,%(listed_date)s,"
                " %(currency)s,%(fiscal_year_end)s,%(notes)s)",
                r,
            )
            for node in r["l3_node"]:
                conn.execute(
                    "INSERT INTO core.entity_node_membership "
                    "(entity_id, l3_node, is_primary, valid_from, known_at, source,"
                    " ingest_run_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        r["entity_id"],
                        node,
                        node == r["primary_node"],
                        r["listed_date"],
                        _known_at_for(r["listed_date"]),
                        SEED_SOURCE,
                        SEED_RUN_ID,
                    ),
                )
    return len(records)


def _load_simple(conn: Connection, csv_path: Path, table: str, columns: list[str]) -> int:
    """按列名直插的简单导入，用于别名 / 指标 / 规则等无派生逻辑的表。

    空单元格对应的列会被整个省略，而不是写入 NULL——这样数据库的 DEFAULT 才会生效。
    node_metric.direction 与 propagation_rule.lag_days 都是 NOT NULL DEFAULT，
    写 NULL 会直接违反约束。
    """
    rows = _read(csv_path)
    if not rows:
        return 0
    missing = set(columns) - set(rows[0])
    if missing:
        raise SeedError(f"{csv_path.name} 缺少列: {sorted(missing)}")
    with conn.transaction():
        for row in rows:
            present = [c for c in columns if row[c] != ""]
            placeholders = ",".join(f"%({c})s" for c in present)
            conn.execute(
                f"INSERT INTO {table} ({','.join(present)}) VALUES ({placeholders})",
                {c: row[c] for c in present},
            )
    return len(rows)


def load_aliases(conn: Connection, csv_path: Path) -> int:
    """导入别名。

    别名一对多是允许的（「中兴」「长城」天然歧义，消歧靠上下文规则），
    但别名等于**另一家实体的全称**一定是录入错误，必须拦下来
    （docs/02-data-model.md §10）。
    """
    rows = _read(csv_path)
    if not rows:
        return 0
    names = {
        str(r[0]): str(r[1])
        for r in conn.execute("SELECT name_full, entity_id FROM core.entity").fetchall()
    }
    for i, row in enumerate(rows, start=2):
        owner = names.get(row.get("alias", ""))
        if owner is not None and owner != row.get("entity_id"):
            raise SeedError(
                f"第 {i} 行别名 {row['alias']!r} 是 {owner} 的全称，"
                f"却挂在 {row.get('entity_id')} 名下"
            )
    return _load_simple(
        conn,
        csv_path,
        "core.entity_alias",
        ["alias", "entity_id", "alias_type", "source", "confidence"],
    )


def load_metrics(conn: Connection, csv_path: Path, taxonomy: Taxonomy | None = None) -> int:
    rows = _read(csv_path)
    tax = _taxonomy_or_default(taxonomy)
    for i, row in enumerate(rows, start=2):
        # l3_node 可空：revenue_total / ai_revenue_pct 这类跨环节指标本来就不挂环节。
        unknown = tax.unknown([row.get("l3_node", "")])
        if unknown:
            raise SeedError(f"第 {i} 行 l3_node 不在环节表中: {unknown}（{row.get('metric_id')}）")
    return _load_simple(
        conn,
        csv_path,
        "core.node_metric",
        [
            "metric_id",
            "l3_node",
            "metric_name",
            "metric_role",
            "frequency",
            "source_type",
            "extraction_hint",
            "direction",
            "definition",
            "unit",
        ],
    )


def load_relations(conn: Connection, csv_path: Path) -> int:
    rows = _read(csv_path)
    with conn.transaction():
        for i, row in enumerate(rows, start=2):
            if not row.get("valid_from"):
                raise SeedError(f"第 {i} 行缺少 valid_from")
            vf = date.fromisoformat(row["valid_from"])
            conn.execute(
                "INSERT INTO core.entity_relation (from_entity, to_entity, relation_type,"
                " strength, share_estimate, direction_note, evidence_type, valid_from,"
                " known_at, source, ingest_run_id, confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["from_entity"],
                    row["to_entity"],
                    row["relation_type"],
                    row["strength"] or None,
                    row["share_estimate"] or None,
                    row["direction_note"] or None,
                    row.get("evidence_type") or "expert",
                    vf,
                    _known_at_for(vf),
                    SEED_SOURCE,
                    SEED_RUN_ID,
                    row.get("confidence") or "medium",
                ),
            )
    return len(rows)


def load_propagation_rules(
    conn: Connection, csv_path: Path, taxonomy: Taxonomy | None = None
) -> int:
    rows = _read(csv_path)
    tax = _taxonomy_or_default(taxonomy)
    for i, row in enumerate(rows, start=2):
        unknown = tax.unknown([row.get("trigger_node", ""), row.get("affected_node", "")])
        if unknown:
            raise SeedError(
                f"第 {i} 行触发/受影响环节不在环节表中: {unknown}"
                f"（规则会匹配不到任何实体，且不会报错）"
            )
    return _load_simple(
        conn,
        csv_path,
        "core.propagation_rule",
        [
            "trigger_node",
            "trigger_event",
            "affected_node",
            "direction",
            "lag_days",
            "mechanism",
            "weight_field",
            "confidence",
            "author",
        ],
    )


_LOADERS: list[tuple[str, str, Callable[[Connection, Path], int]]] = [
    ("entity.csv", "core.entity", load_entities),
    ("entity_alias.csv", "core.entity_alias", load_aliases),
    ("node_metric.csv", "core.node_metric", load_metrics),
    ("entity_relation.csv", "core.entity_relation", load_relations),
    ("propagation_rule.csv", "core.propagation_rule", load_propagation_rules),
]


def load_all(conn: Connection, seed_dir: Path) -> dict[str, int]:
    """按依赖顺序导入 seed_dir 下存在的 CSV，返回每张表的导入行数。"""
    counts: dict[str, int] = {}
    for filename, table, fn in _LOADERS:
        path = seed_dir / filename
        if path.exists():
            counts[table] = fn(conn, path)
    return counts
