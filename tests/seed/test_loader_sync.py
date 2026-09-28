"""种子重复导入 = 同步：库里已经有真实数据（财务、行情），不能清库重来。

时点表（环节归属、关系）只追加、不物理删除：种子里去掉的行记 superseded_at = 导入时刻；
已有实体上新增的环节从导入时刻起才算已知（不回溯）。指标口径变了发新版本、去掉了就
让现行版本失效。传导规则去掉的置 enabled = false。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.seed.loader import SeedError, load_all
from ragdemo_core.db.migrate import migrate

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

ENTITY_HEADER = [
    "entity_id",
    "name_full",
    "name_short",
    "name_en",
    "entity_type",
    "market",
    "tushare_code",
    "wind_code",
    "edgar_cik",
    "l1_layer",
    "l2_segment",
    "l3_node",
    "primary_node",
    "hq_country",
    "status",
    "listed_date",
    "valid_from",
    "currency",
    "fiscal_year_end",
    "notes",
]
HYGON = {
    "entity_id": "CN.688041", "name_full": "海光信息技术股份有限公司", "name_short": "海光信息",
    "entity_type": "listed", "market": "SSE", "tushare_code": "688041.SH", "l1_layer": "算力",
    "l2_segment": "AI芯片", "l3_node": "通用服务器CPU", "primary_node": "通用服务器CPU",
    "hq_country": "CN", "status": "active", "listed_date": "2022-08-12", "currency": "CNY",
    "fiscal_year_end": "12",
}  # fmt: skip
SUGON = {
    **HYGON, "entity_id": "CN.603019", "name_full": "曙光信息产业股份有限公司",
    "name_short": "中科曙光", "tushare_code": "603019.SH", "l2_segment": "服务器与网络",
    "l3_node": "AI服务器", "primary_node": "AI服务器", "listed_date": "2014-11-06",
}  # fmt: skip
OPENAI = {
    **HYGON, "entity_id": "US.OPENAI", "name_full": "OpenAI OpCo LLC", "name_short": "OpenAI",
    "entity_type": "private", "market": "", "tushare_code": "", "l1_layer": "模型",
    "l2_segment": "基础模型", "l3_node": "通用大模型", "primary_node": "通用大模型",
    "hq_country": "US", "listed_date": "", "valid_from": "2026-09-28", "currency": "USD",
}  # fmt: skip
REL_HEADER = [
    "from_entity",
    "to_entity",
    "relation_type",
    "strength",
    "share_estimate",
    "direction_note",
    "evidence_type",
    "valid_from",
    "confidence",
]
METRIC_HEADER = [
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
]
RULE_HEADER = [
    "trigger_node",
    "trigger_event",
    "affected_node",
    "direction",
    "lag_days",
    "mechanism",
    "weight_field",
    "confidence",
    "author",
]


def _write(path: Path, header: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(header))
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


def _rel(src: str, dst: str, kind: str = "supplies_to", vf: str = "2026-09-28") -> dict[str, str]:
    return {"from_entity": src, "to_entity": dst, "relation_type": kind, "valid_from": vf}


def _metric(metric_id: str, definition: str = "口径") -> dict[str, str]:
    return {
        "metric_id": metric_id, "metric_name": metric_id, "metric_role": "confirming",
        "frequency": "quarterly", "source_type": "filing", "definition": definition, "unit": "CNY",
    }  # fmt: skip


def _rule(affected: str) -> dict[str, str]:
    return {
        "trigger_node": "AI服务器", "trigger_event": "e", "affected_node": affected,
        "direction": "1", "lag_days": "90", "mechanism": "机制", "confidence": "high",
        "author": "docs/csv#9",
    }  # fmt: skip


def _seed(
    d: Path,
    *,
    entities: Sequence[Mapping[str, str]] = (HYGON, SUGON, OPENAI),
    relations: Sequence[Mapping[str, str]] = (),
    metrics: Sequence[Mapping[str, str]] = (),
    rules: Sequence[Mapping[str, str]] = (),
) -> Path:
    d.mkdir(exist_ok=True)
    _write(d / "entity.csv", ENTITY_HEADER, entities)
    _write(d / "entity_relation.csv", REL_HEADER, relations)
    _write(d / "node_metric.csv", METRIC_HEADER, metrics)
    _write(d / "propagation_rule.csv", RULE_HEADER, rules)
    return d


@pytest.fixture
def conn(temp_db: str) -> Iterator[psycopg.Connection]:
    c = psycopg.connect(temp_db)
    migrate(c, Path("db/migrations"))
    c.commit()
    yield c
    c.close()


def _one(conn: psycopg.Connection, sql: str, *args: object) -> object:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.db
def test_reloading_the_same_seed_changes_nothing(conn: psycopg.Connection, tmp_path: Path) -> None:
    d = _seed(
        tmp_path / "s",
        relations=[_rel("CN.688041", "CN.603019")],
        metrics=[_metric("revenue_total")],
        rules=[_rule("液冷散热")],
    )
    load_all(conn, d, now=NOW)
    load_all(conn, d, now=LATER)
    assert _one(conn, "SELECT count(*) FROM core.entity") == 3
    assert _one(conn, "SELECT count(*) FROM core.entity_node_membership") == 3
    assert _one(conn, "SELECT count(*) FROM core.entity_relation") == 1
    assert _one(conn, "SELECT count(*) FROM core.node_metric_version") == 1
    assert _one(conn, "SELECT count(*) FROM core.propagation_rule") == 1


@pytest.mark.db
def test_private_entity_needs_no_listing_date_but_listed_does(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    load_all(conn, _seed(tmp_path / "a"), now=NOW)
    assert _one(conn, "SELECT listed_date FROM core.entity WHERE entity_id = 'US.OPENAI'") is None
    with pytest.raises(SeedError, match="listed_date"):
        load_all(conn, _seed(tmp_path / "b", entities=[{**HYGON, "listed_date": ""}]), now=NOW)


@pytest.mark.db
def test_entity_fields_are_updated_and_a_new_node_is_known_only_from_now(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    load_all(conn, _seed(tmp_path / "a"), now=NOW)
    moved = {**HYGON, "listed_date": "2022-08-13", "l3_node": "通用服务器CPU|云端训练芯片"}
    load_all(conn, _seed(tmp_path / "b", entities=[moved, SUGON, OPENAI]), now=LATER)
    listed = _one(conn, "SELECT listed_date::text FROM core.entity WHERE entity_id = 'CN.688041'")
    assert listed == "2022-08-13"
    known = _one(
        conn,
        "SELECT known_at FROM core.entity_node_membership"
        " WHERE entity_id = 'CN.688041' AND l3_node = '云端训练芯片'",
    )
    assert known == LATER, "已有实体新增的环节从导入时刻起才算已知，不回溯到上市日"


@pytest.mark.db
def test_removed_node_is_superseded_not_deleted(conn: psycopg.Connection, tmp_path: Path) -> None:
    two = {**HYGON, "l3_node": "通用服务器CPU|云端训练芯片"}
    load_all(conn, _seed(tmp_path / "a", entities=[two, SUGON, OPENAI]), now=NOW)
    load_all(conn, _seed(tmp_path / "b"), now=LATER)
    row = conn.execute(
        "SELECT superseded_at FROM core.entity_node_membership"
        " WHERE entity_id = 'CN.688041' AND l3_node = '云端训练芯片'"
    ).fetchone()
    assert row == (LATER,)


@pytest.mark.db
def test_relation_sync_supersedes_removed_adds_new_and_keeps_same_triple(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    old = [
        _rel("CN.688041", "CN.603019", vf="2022-08-12"),
        _rel("CN.603019", "CN.688041", "invests_in"),
    ]
    load_all(conn, _seed(tmp_path / "a", relations=old), now=NOW)
    new = [_rel("CN.688041", "CN.603019", vf="2026-09-28"), _rel("CN.688041", "US.OPENAI")]
    load_all(conn, _seed(tmp_path / "b", relations=new), now=LATER)
    live = conn.execute(
        "SELECT from_entity, to_entity, relation_type, valid_from::text FROM core.entity_relation"
        " WHERE superseded_at IS NULL ORDER BY 1, 2"
    ).fetchall()
    assert live == [
        ("CN.688041", "CN.603019", "supplies_to", "2022-08-12"),  # 同一三元组沿用原行
        ("CN.688041", "US.OPENAI", "supplies_to", "2026-09-28"),
    ]
    gone = _one(
        conn,
        "SELECT superseded_at FROM core.entity_relation WHERE relation_type = 'invests_in'",
    )
    assert gone == LATER


@pytest.mark.db
def test_metric_change_is_a_new_version_now_and_removal_retires_it(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    first = [_metric("revenue_total"), _metric("cowos_capacity")]
    load_all(conn, _seed(tmp_path / "a", metrics=first), now=NOW)
    load_all(conn, _seed(tmp_path / "b", metrics=[_metric("revenue_total", "新口径")]), now=LATER)
    live = conn.execute(
        "SELECT metric_id, version, known_at FROM core.node_metric_version"
        " WHERE superseded_at IS NULL"
    ).fetchall()
    assert live == [("revenue_total", 2, LATER)]
    retired = _one(
        conn,
        "SELECT superseded_at FROM core.node_metric_version WHERE metric_id = 'cowos_capacity'",
    )
    assert retired == LATER


@pytest.mark.db
def test_removed_rule_is_disabled_not_deleted(conn: psycopg.Connection, tmp_path: Path) -> None:
    load_all(conn, _seed(tmp_path / "a", rules=[_rule("液冷散热"), _rule("服务器电源")]), now=NOW)
    load_all(conn, _seed(tmp_path / "b", rules=[_rule("液冷散热")]), now=LATER)
    rows = conn.execute(
        "SELECT affected_node, enabled FROM core.propagation_rule ORDER BY 1"
    ).fetchall()
    assert rows == [("服务器电源", False), ("液冷散热", True)]
