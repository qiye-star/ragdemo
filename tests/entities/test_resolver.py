"""实体解析三层降级：代码 → 别名 → 上下文。三层都不确定则进人工队列，不猜。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo.entities.resolver import EntityResolver
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


@pytest.fixture()
def resolver(temp_db: str) -> EntityResolver:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, name_short, entity_type,"
        " l1_layer, l2_segment, l3_node, primary_node, tushare_code) VALUES "
        "('CN.688256','寒武纪-U','寒武纪','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH'),"
        "('CN.000063','中兴通讯','中兴','listed','算力','通信设备',"
        " ARRAY['服务器'],'服务器','000063.SZ'),"
        "('CN.002371','北方华创','北方华创','listed','算力','半导体设备',"
        " ARRAY['前道设备'],'前道设备','002371.SZ')"
    )
    conn.execute(
        "INSERT INTO core.entity_alias (alias, entity_id, alias_type, source) VALUES "
        "('寒武纪','CN.688256','short','manual'),"
        "('Cambricon','CN.688256','en','manual'),"
        "('中兴','CN.000063','short','manual'),"
        "('中兴','CN.002371','nickname','manual')"  # 故意制造一对多
    )
    conn.commit()
    return EntityResolver(conn)


@pytest.mark.db
def test_layer1_code_match_is_high_confidence(resolver: EntityResolver) -> None:
    r = resolver.resolve_code("688256.SH")
    assert (r.entity_id, r.layer, r.confidence) == ("CN.688256", "code", "high")


@pytest.mark.db
def test_layer1_unknown_code_resolves_to_none(resolver: EntityResolver) -> None:
    assert resolver.resolve_code("999999.SH").entity_id is None


@pytest.mark.db
def test_layer2_unique_alias_resolves(resolver: EntityResolver) -> None:
    r = resolver.resolve_name("寒武纪")
    assert (r.entity_id, r.layer) == ("CN.688256", "alias")


@pytest.mark.db
def test_layer3_ambiguous_alias_uses_context(resolver: EntityResolver) -> None:
    """「中兴」一对多，靠同文档共现实体消歧。"""
    r = resolver.resolve_name("中兴", context_entities=["CN.002371"])
    assert (r.entity_id, r.layer) == ("CN.002371", "context")


@pytest.mark.db
def test_ambiguous_without_context_returns_candidates_not_a_guess(
    resolver: EntityResolver,
) -> None:
    """没有上下文就不猜——把候选交出去，由人来定。"""
    r = resolver.resolve_name("中兴")
    assert r.entity_id is None
    assert {c.entity_id for c in r.candidates} == {"CN.000063", "CN.002371"}


@pytest.mark.db
def test_fuzzy_match_is_never_auto_accepted(resolver: EntityResolver) -> None:
    """「中兴新材」不得被自动判成「中兴通讯」。这类污染会扩散到关系图和观点。"""
    r = resolver.resolve_name("中兴新材")
    assert r.entity_id is None
    assert r.confidence == "low"


@pytest.mark.db
def test_unresolved_goes_to_the_queue(resolver: EntityResolver) -> None:
    r = resolver.resolve_name("中兴")
    qid = resolver.enqueue_unresolved(
        r, raw_ref="中兴", context={"doc_id": 1}, source="mock", ingest_run_id="r1"
    )
    row = resolver.conn.execute(
        "SELECT raw_ref, candidates, resolved_at FROM core.entity_resolution_queue WHERE id = %s",
        (qid,),
    ).fetchone()
    assert row is not None
    assert row[0] == "中兴"
    assert len(row[1]) == 2
    assert row[2] is None
