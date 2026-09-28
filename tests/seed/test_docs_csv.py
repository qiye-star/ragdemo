"""docs/csv → db/seed 的转换（离线、确定性）。

对真实的 docs/csv 与 db/seed/sources 跑一遍，守住会悄悄出错的几条规则：
名字解析必须严格、泛称不能变成实体、关系日期不能回溯、源表的错列要按规则还原、
方向 / 时滞 / 强度按原文解析。
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from ragdemo.seed.docs_csv import BuildError, build_seed, parse_lag_days, repair_rule_row
from ragdemo.seed.taxonomy import load_taxonomy

DOCS = Path("docs/csv")
SOURCES = Path("db/seed/sources")
SEED = Path("db/seed")


@pytest.fixture(scope="module")
def built():  # type: ignore[no-untyped-def]  # noqa: ANN201
    return build_seed(DOCS, SOURCES, SEED)


def test_every_company_in_the_list_becomes_an_entity(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    entities = built.files["entity.csv"]
    ids = [e["entity_id"] for e in entities]
    assert len(ids) == len(set(ids)) == 118  # 100 + 关系表补录 14 + 原种子保留 4
    by_id = {e["entity_id"]: e for e in entities}
    assert by_id["CN.688041"]["name_short"] == "海光信息"
    assert by_id["CN.688825"]["entity_type"] == "listed", "长鑫按供应商数据已上市"
    assert (
        by_id["US.OPENAI"]["listed_date"] == "" and by_id["US.OPENAI"]["entity_type"] == "private"
    )


def test_listed_entities_have_provider_listing_dates(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    listed = [e for e in built.files["entity.csv"] if e["entity_type"] == "listed"]
    assert all(e["listed_date"] for e in listed)
    by_id = {e["entity_id"]: e for e in built.files["entity.csv"]}
    assert by_id["CN.688041"]["listed_date"] == "2022-08-12"


def test_all_nodes_are_in_the_taxonomy(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    tax = load_taxonomy(SEED / "taxonomy.csv")
    for e in built.files["entity.csv"]:
        assert not tax.unknown([*e["l3_node"].split("|"), e["primary_node"]]), e["entity_id"]
    for r in built.files["propagation_rule.csv"]:
        assert not tax.unknown([r["trigger_node"], r["affected_node"]]), r
    for m in built.files["node_metric.csv"]:
        assert not tax.unknown([m["l3_node"]]), m["metric_id"]


def test_relations_resolve_aliases_and_drop_groups_into_notes(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    rel = built.files["entity_relation.csv"]
    keys = {(r["from_entity"], r["relation_type"], r["to_entity"]) for r in rel}
    assert ("NL.ASML", "supplies_to", "TW.2330") in keys, "ASML → 阿斯麦"
    assert ("US.AMZN", "invests_in", "US.ANTHROPIC") in keys
    assert ("US.AMZN", "supplies_to", "US.ANTHROPIC") in keys, "投资/供货拆成两条"
    assert all(r["from_entity"] != r["to_entity"] for r in rel), "平头哥→阿里云 合并后是自己"
    notes = {e["entity_id"]: e["notes"] for e in built.files["entity.csv"]}
    assert "北美云厂" in notes["CN.601138"], "工业富联 供货 北美云厂：泛称写进备注"
    assert len(rel) >= 150


def test_new_relations_are_not_backdated(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    rel = built.files["entity_relation.csv"]
    asml = next(r for r in rel if (r["from_entity"], r["to_entity"]) == ("NL.ASML", "TW.2330"))
    assert asml["valid_from"] == "2026-09-28"
    evidence = Counter(r["evidence_type"] for r in rel)
    assert set(evidence) <= {"announcement", "filing", "news", "expert", "inferred"}  # 表结构取值


def test_unknown_relation_name_fails_the_build(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(DOCS, docs)
    path = docs / "AI产业链_02_公司关系.csv"
    path.chmod(0o644)  # docs/csv 是只读的，copytree 连权限一起复制
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(text + "201,某不存在的公司,供货,台积电,x,L1,行业共识,x\n", encoding="utf-8-sig")
    with pytest.raises(BuildError, match="某不存在的公司"):
        build_seed(docs, SOURCES, SEED)


def test_metrics_keep_financials_and_take_the_dictionary(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    ids = {m["metric_id"] for m in built.files["node_metric.csv"]}
    assert len(ids) == 43
    assert {"revenue_total", "net_profit_attributable", "capex"} <= ids
    assert {"cowos_capacity", "hyperscaler_capex_total", "token_volume"} <= ids
    assert "optical_module_800g_shipment" not in ids
    cowos = next(m for m in built.files["node_metric.csv"] if m["metric_id"] == "cowos_capacity")
    assert cowos["metric_role"] == "leading" and "法说会" in cowos["extraction_hint"]


def test_rules_expand_to_node_pairs_and_keep_their_failure_conditions(built) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    rules = built.files["propagation_rule.csv"]
    first = [r for r in rules if r["author"] == "docs/csv#1"]
    assert {(r["trigger_node"], r["affected_node"]) for r in first} == {
        ("公有云", "光模块"),
        ("公有云", "交换机"),
    }
    assert all(r["direction"] == "1" and r["lag_days"] == "90" for r in first)
    assert "失效条件" in first[0]["mechanism"] and "土建与电力" in first[0]["mechanism"]
    assert {r["direction"] for r in rules if r["author"] == "docs/csv#11"} == {"0"}
    assert {r["lag_days"] for r in rules if r["author"] == "docs/csv#5"} == {"0"}
    assert {r["confidence"] for r in rules if r["author"] == "docs/csv#12"} == {"low"}


def test_malformed_rule_row_is_repaired_by_joining_the_mechanism() -> None:
    row = [
        "2",
        "事件",
        "L4算力",
        "L2设计",
        "利好",
        "1-3个季度",
        "甲",
        "乙",
        "丙",
        "强",
        "验证",
        "失效",
    ]
    fixed = repair_rule_row(row, expected=10)
    assert fixed[6] == "甲，乙，丙" and fixed[7:] == ["强", "验证", "失效"]


@pytest.mark.parametrize(
    ("text", "days"),
    [("1-2个季度", 90), ("2-4个季度", 180), ("同期", 0), ("即时至2个季度", 0), ("4-8个季度", 360)],
)
def test_lag_takes_the_lower_bound(text: str, days: int) -> None:
    assert parse_lag_days(text) == days
