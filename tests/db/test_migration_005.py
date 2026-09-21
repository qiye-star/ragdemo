"""迁移 005：观点、评测、审计。重点是「观点必须有依据」与「两轨不合并」。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_OPINION = (
    "INSERT INTO core.opinion "
    "(entity_id, direction, confidence, thesis, evidence_blocks, evidence_facts, "
    " agent_name, prompt_version, model, run_id, as_of) "
    "VALUES ('CN.688256','bull','medium','测试论点',%s,%s,'fundamental','v1','m','r1',"
    " '2024-10-29 09:00+08') RETURNING opinion_id"
)


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(
            "INSERT INTO core.entity "
            "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
            "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
            " ARRAY['云端训练芯片'],'云端训练芯片')"
        )
        conn.commit()
        yield conn


@pytest.mark.db
def test_opinion_without_evidence_is_rejected(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """合规硬约束的最后一道防线（CLAUDE.md §0）。"""
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        db.execute(_OPINION, ([], []))


@pytest.mark.db
def test_opinion_with_evidence_is_accepted(db: psycopg.Connection[tuple[object, ...]]) -> None:
    with db.transaction(force_rollback=True):
        row = db.execute(_OPINION, ([1, 2], [])).fetchone()
    assert row is not None


@pytest.mark.db
def test_price_and_evidence_tracks_coexist(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """adr/0007：两轨分开存，UNIQUE 是 (opinion_id, track, horizon)。"""
    with db.transaction(force_rollback=True):
        row = db.execute(_OPINION, ([1], [])).fetchone()
        assert row is not None
        oid = row[0]
        ins = (
            "INSERT INTO core.opinion_score "
            "(opinion_id, track, horizon, scored_at, score, outcome_desc, scorer) "
            "VALUES (%s, %s, '1M', now(), 1, '测试', %s)"
        )
        db.execute(ins, (oid, "price", "auto"))
        db.execute(ins, (oid, "evidence", "human"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(ins, (oid, "price", "auto"))


@pytest.mark.db
def test_bitemporal_registry_lists_seven_tables(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = db.execute("SELECT table_name::text FROM core.bitemporal_registry").fetchall()
    assert {r[0] for r in rows} == {
        "core.fin_fact",
        "core.price_daily",
        "core.document",
        "core.doc_block",
        "core.entity_relation",
        "core.entity_node_membership",
        "core.event",
    }


@pytest.mark.db
def test_entity_resolution_queue_exists(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """docs/04-ingestion.md §4：三层降级的兜底出口。"""
    row = db.execute("SELECT to_regclass('core.entity_resolution_queue') IS NOT NULL").fetchone()
    assert row is not None
    assert row[0] is True
