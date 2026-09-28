"""种子 CSV 导入：先全量校验，再整体写入。

「先校验后写入」而不是边读边写：一份 CSV 里有一行错就整份拒绝。
部分导入的种子数据比没有种子数据更糟——它看起来成功了，实际缺了一批实体，
而下游的关系导入会因为外键失败得莫名其妙。
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from ragdemo.metrics.definitions import (
    METRIC_BASELINE_KNOWN_AT,
    MetricSpec,
    SourceMapSpec,
    publish_metric,
    publish_source_map,
    retire_metric,
)
from ragdemo.seed.taxonomy import DEFAULT_TAXONOMY_PATH, Taxonomy, load_taxonomy

ENTITY_ID_RE = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9._-]{1,32}$")
# 手工数据统一按 UTC 当日 00:00 记，见 docs/03-point-in-time.md §6.3
SEED_TZ = UTC
SEED_RUN_ID = "seed"
SEED_SOURCE = "manual:seed"

# 这些列在 core.entity 里可空。空单元格必须写 NULL 而不是空串：
# tushare_code 带 UNIQUE 约束，多家海外实体都写 '' 会在第二行就撞唯一键。
NULLABLE_ENTITY_COLUMNS = frozenset(
    {
        "name_short",
        "name_en",
        "market",
        "tushare_code",
        "wind_code",
        "edgar_cik",
        "hq_country",
        "currency",
        "notes",
    }
)

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
        listed = row.get("entity_type", "listed") == "listed"
        if listed and not row["listed_date"]:
            raise SeedError(f"第 {i} 行缺少 listed_date: {eid}（观点评分需要它剔除次新股）")
        valid_from = row.get("valid_from") or row["listed_date"]
        if not valid_from:
            raise SeedError(f"第 {i} 行非上市实体要给 valid_from（环节归属的起点）: {eid}")

        out.append(
            {
                **{k: (v or None) for k, v in row.items() if k in NULLABLE_ENTITY_COLUMNS},
                **{k: v for k, v in row.items() if k not in NULLABLE_ENTITY_COLUMNS},
                "l3_node": nodes,
                "listed_date": date.fromisoformat(row["listed_date"])
                if row["listed_date"]
                else None,
                "valid_from": date.fromisoformat(valid_from),
                "fiscal_year_end": int(row["fiscal_year_end"]) if row["fiscal_year_end"] else None,
            }
        )
    return out


_ENTITY_COLUMNS = (
    "entity_id", "name_full", "name_short", "name_en", "entity_type", "market", "tushare_code",
    "wind_code", "edgar_cik", "l1_layer", "l2_segment", "l3_node", "primary_node", "hq_country",
    "status", "listed_date", "currency", "fiscal_year_end", "notes",
)  # fmt: skip


def load_entities(
    conn: Connection,
    csv_path: Path,
    taxonomy: Taxonomy | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """导入 / 同步实体，并同步每个实体的环节归属（时点表）。

    `core.entity` 是"当前画像"表，已有的行直接更新。环节归属是时点表：
    - 新实体的归属 known_at 取 valid_from 当日 00:00（种子的既有约定，03 §6.3）；
    - 已有实体新增的环节从导入时刻 `now` 起才算已知——不回溯到上市日；
    - 种子里去掉的环节（或主环节换了）记 superseded_at = now，不删行。
    CSV 里没有、库里有的实体不动：它可能已经挂着财务 / 行情数据。
    """
    records = _validate_entities(_read(csv_path), _taxonomy_or_default(taxonomy))
    now = now or datetime.now(UTC)
    columns = ", ".join(_ENTITY_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ENTITY_COLUMNS if c != "entity_id")
    with conn.transaction():
        for r in records:
            existed = conn.execute(
                "SELECT 1 FROM core.entity WHERE entity_id = %s", (r["entity_id"],)
            ).fetchone()
            conn.execute(
                f"INSERT INTO core.entity ({columns}) VALUES"
                f" ({', '.join(f'%({c})s' for c in _ENTITY_COLUMNS)})"
                f" ON CONFLICT (entity_id) DO UPDATE SET {updates}, updated_at = now()",
                r,
            )
            _sync_membership(conn, r, is_new=existed is None, now=now)
    return len(records)


def _sync_membership(conn: Connection, r: dict[str, Any], *, is_new: bool, now: datetime) -> None:
    desired = {node: node == r["primary_node"] for node in r["l3_node"]}
    live = {
        str(row[0]): bool(row[1])
        for row in conn.execute(
            "SELECT l3_node, is_primary FROM core.entity_node_membership"
            " WHERE entity_id = %s AND superseded_at IS NULL",
            (r["entity_id"],),
        ).fetchall()
    }
    for node, primary in live.items():
        if desired.get(node) != primary:
            conn.execute(
                "UPDATE core.entity_node_membership SET superseded_at = %s"
                " WHERE entity_id = %s AND l3_node = %s AND superseded_at IS NULL",
                (now, r["entity_id"], node),
            )
    for node, primary in desired.items():
        if live.get(node) == primary:
            continue
        valid_from, known_at = (
            (r["valid_from"], _known_at_for(r["valid_from"])) if is_new else (now.date(), now)
        )
        conn.execute(
            "INSERT INTO core.entity_node_membership "
            "(entity_id, l3_node, is_primary, valid_from, known_at, source, ingest_run_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (r["entity_id"], node, primary, valid_from, known_at, SEED_SOURCE, SEED_RUN_ID),
        )


def _load_simple(
    conn: Connection, csv_path: Path, table: str, columns: list[str], *, on_conflict: str = ""
) -> int:
    """按列名直插的简单导入，用于别名这类无派生逻辑的表。

    空单元格对应的列会被整个省略，而不是写入 NULL——这样数据库的 DEFAULT 才会生效。
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
                f"INSERT INTO {table} ({','.join(present)}) VALUES ({placeholders}) {on_conflict}",
                {c: row[c] for c in present},
            )
    return len(rows)


def load_aliases(conn: Connection, csv_path: Path, *, now: datetime | None = None) -> int:
    """导入别名（重复导入是空操作）。

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
        on_conflict="ON CONFLICT (alias, entity_id) DO NOTHING",
    )


def load_metrics(
    conn: Connection,
    csv_path: Path,
    taxonomy: Taxonomy | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """指标定义经口径版本化入口写入 / 同步（迁移 018）。

    - 新指标：版本 1，known_at 取固定基线 METRIC_BASELINE_KNOWN_AT，任何历史 as_of 下
      都可见——与种子实体 known_at 取 valid_from 是同一个有意接受的乐观偏差（03 §6.3）；
    - 口径变了：发新版本，known_at = 导入时刻 `now`（口径变更是现在才知道的）；
    - 种子里去掉的：现行版本在 `now` 失效（retire_metric），历史版本保留。
    """
    rows = _read(csv_path)
    tax = _taxonomy_or_default(taxonomy)
    now = now or datetime.now(UTC)
    for i, row in enumerate(rows, start=2):
        # l3_node 可空：revenue_total / ai_revenue_pct 这类跨环节指标本来就不挂环节。
        unknown = tax.unknown([row.get("l3_node", "")])
        if unknown:
            raise SeedError(f"第 {i} 行 l3_node 不在环节表中: {unknown}（{row.get('metric_id')}）")
    missing = set(_METRIC_CSV_COLUMNS) - set(rows[0]) if rows else set()
    if missing:
        raise SeedError(f"{csv_path.name} 缺少列: {sorted(missing)}")
    live = {
        str(r[0])
        for r in conn.execute(
            "SELECT metric_id FROM core.node_metric_version WHERE superseded_at IS NULL"
        ).fetchall()
    }
    with conn.transaction():
        for row in rows:
            spec = MetricSpec(
                metric_id=row["metric_id"],
                metric_name=row["metric_name"],
                metric_role=row["metric_role"],
                frequency=row["frequency"],
                source_type=row["source_type"],
                definition=row["definition"],
                unit=row["unit"],
                l3_node=row["l3_node"] or None,
                extraction_hint=row["extraction_hint"] or None,
                direction=int(row["direction"]) if row["direction"] else 1,
            )
            existing = spec.metric_id in live
            publish_metric(
                conn,
                spec,
                author="seed",
                known_at=now if existing else METRIC_BASELINE_KNOWN_AT,
                source=SEED_SOURCE,
                ingest_run_id="seed",
                change_note="种子同步" if existing else None,
            )
        for metric_id in sorted(live - {r["metric_id"] for r in rows}):
            retire_metric(conn, metric_id, known_at=now)
    return len(rows)


_METRIC_CSV_COLUMNS = (
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
)


def load_source_maps(conn: Connection, csv_path: Path, *, now: datetime | None = None) -> int:
    """供应商字段映射经口径版本化入口写入（迁移 018）：版本 1 取固定基线时刻。
    幂等——已存在且内容相同的行不产生新版本，可以对已经种过数据的库重复执行。"""
    rows = _read(csv_path)
    with conn.transaction():
        for row in rows:
            publish_source_map(
                conn,
                SourceMapSpec(
                    metric_id=row["metric_id"],
                    provider=row["provider"],
                    provider_field=row["provider_field"],
                    priority=int(row["priority"] or 100),
                    scale_factor=Decimal(row["scale_factor"] or "1"),
                    notes=row["notes"] or None,
                ),
                author="seed",
                known_at=METRIC_BASELINE_KNOWN_AT,
                source=SEED_SOURCE,
                ingest_run_id="seed",
            )
    return len(rows)


def load_relations(conn: Connection, csv_path: Path, *, now: datetime | None = None) -> int:
    """导入 / 同步实体关系（时点表）。

    按 (from, to, 类型) 同步：库里已有同一三元组的现行种子关系就沿用它（不因 valid_from
    不同而多出一条"同时有效"的重复）；种子里去掉的现行种子关系记 superseded_at = now。
    只动 source = manual:seed 的行——别的来源写入的关系不归种子管。
    """
    rows = _read(csv_path)
    now = now or datetime.now(UTC)
    for i, row in enumerate(rows, start=2):
        if not row.get("valid_from"):
            raise SeedError(f"第 {i} 行缺少 valid_from")
    wanted = {(r["from_entity"], r["to_entity"], r["relation_type"]) for r in rows}
    with conn.transaction():
        live = {
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT from_entity, to_entity, relation_type::text FROM core.entity_relation"
                " WHERE superseded_at IS NULL AND source = %s",
                (SEED_SOURCE,),
            ).fetchall()
        }
        for src, dst, kind in sorted(live - wanted):
            conn.execute(
                "UPDATE core.entity_relation SET superseded_at = %s"
                " WHERE from_entity = %s AND to_entity = %s AND relation_type = %s"
                "   AND superseded_at IS NULL AND source = %s",
                (now, src, dst, kind, SEED_SOURCE),
            )
        for row in rows:
            if (row["from_entity"], row["to_entity"], row["relation_type"]) in live:
                continue
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


_RULE_COLUMNS = (
    "trigger_node",
    "trigger_event",
    "affected_node",
    "direction",
    "lag_days",
    "mechanism",
    "weight_field",
    "confidence",
    "author",
)


def _rule_key(values: Sequence[object]) -> tuple[str, ...]:
    return tuple("" if v is None else str(v) for v in values)


def load_propagation_rules(
    conn: Connection,
    csv_path: Path,
    taxonomy: Taxonomy | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """导入 / 同步传导规则。规则表不是时点表，靠 enabled 开关：内容完全相同的现行规则
    不重复插入；种子里去掉的现行规则置 enabled = false（不删行，旧观点的解释还引用它）。"""
    rows = _read(csv_path)
    tax = _taxonomy_or_default(taxonomy)
    for i, row in enumerate(rows, start=2):
        unknown = tax.unknown([row.get("trigger_node", ""), row.get("affected_node", "")])
        if unknown:
            raise SeedError(
                f"第 {i} 行触发/受影响环节不在环节表中: {unknown}"
                f"（规则会匹配不到任何实体，且不会报错）"
            )
    wanted = {_rule_key([row[c] for c in _RULE_COLUMNS]) for row in rows}
    with conn.transaction():
        live = {
            _rule_key(r[1:]): int(str(r[0]))
            for r in conn.execute(
                f"SELECT rule_id, {', '.join(_RULE_COLUMNS)} FROM core.propagation_rule"
                " WHERE enabled"
            ).fetchall()
        }
        for k, rule_id in live.items():
            if k not in wanted:
                conn.execute(
                    "UPDATE core.propagation_rule SET enabled = false WHERE rule_id = %s",
                    (rule_id,),
                )
        for row in rows:
            if _rule_key([row[c] for c in _RULE_COLUMNS]) in live:
                continue
            present = [c for c in _RULE_COLUMNS if row[c] != ""]
            conn.execute(
                f"INSERT INTO core.propagation_rule ({', '.join(present)})"
                f" VALUES ({', '.join(f'%({c})s' for c in present)})",
                {c: row[c] for c in present},
            )
    return len(rows)


_LOADERS: list[tuple[str, str, Callable[..., int]]] = [
    ("entity.csv", "core.entity", load_entities),
    ("entity_alias.csv", "core.entity_alias", load_aliases),
    ("node_metric.csv", "core.node_metric", load_metrics),
    ("metric_source_map.csv", "core.metric_source_map", load_source_maps),
    ("entity_relation.csv", "core.entity_relation", load_relations),
    ("propagation_rule.csv", "core.propagation_rule", load_propagation_rules),
]


def load_all(conn: Connection, seed_dir: Path, *, now: datetime | None = None) -> dict[str, int]:
    """按依赖顺序导入 / 同步 seed_dir 下存在的 CSV，返回每张表的行数。重复执行是安全的。"""
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for filename, table, fn in _LOADERS:
        path = seed_dir / filename
        if path.exists():
            counts[table] = fn(conn, path, now=now)
    return counts
