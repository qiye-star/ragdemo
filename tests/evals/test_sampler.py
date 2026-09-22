"""评测集分层抽样：确定性、每层保底、总数逼近 n。"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg
import pytest

from ragdemo.evals.sampler import sample_candidates, write_candidates_csv
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


def _seed_block(
    conn: psycopg.Connection,
    *,
    block_id: int,
    doc_id: int,
    entity_id: str,
    doc_type: str,
    parse_engine: str,
) -> None:
    conn.execute(
        "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at, source,"
        " content_hash, version_group_id, valid_from, known_at, ingest_run_id, parse_engine) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,'t','2024-10-28 10:00+00','mock',%s,%s,"
        "'2024-07-01','2024-10-28 10:00+00','r1',%s) "
        "ON CONFLICT (doc_id) DO NOTHING",
        (doc_id, entity_id, doc_type, f"h{doc_id}", doc_id, parse_engine),
    )
    conn.execute(
        "INSERT INTO core.doc_block (block_id, doc_id, block_type, section_path, ordinal,"
        " content, is_leaf, entity_id, doc_type, publish_at, valid_from, known_at, source,"
        " ingest_run_id) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,%s,'paragraph','',%s,%s,true,%s,%s,"
        "'2024-10-28 10:00+00','2024-07-01','2024-10-28 10:00+00','mock','r1')",
        (block_id, doc_id, block_id, f"内容 {block_id}", entity_id, doc_type),
    )
    conn.commit()


@pytest.fixture()
def conn(temp_db: str) -> psycopg.Connection:
    c = psycopg.connect(temp_db)
    migrate(c, MIGRATIONS)
    for entity_id, name in (("CN.A", "甲公司"), ("CN.B", "乙公司")):
        c.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
            " l2_segment, l3_node, primary_node) "
            "VALUES (%s,%s,'listed','算力','AI芯片',ARRAY['x'],'x')",
            (entity_id, name),
        )
    c.commit()
    return c


def _seed_diverse_pool(conn: psycopg.Connection) -> None:
    """三个分层：(quarterly, textin, CN.A) 10 条、(announcement, textin, CN.A)
    2 条（不够 min_per_stratum，测"有多少拿多少"）、(quarterly, mock, CN.B) 8 条。"""
    block_id = 1
    for i in range(10):
        _seed_block(
            conn, block_id=block_id, doc_id=100 + i, entity_id="CN.A",
            doc_type="quarterly", parse_engine="textin:1+fp1",
        )
        block_id += 1
    for i in range(2):
        _seed_block(
            conn, block_id=block_id, doc_id=200 + i, entity_id="CN.A",
            doc_type="announcement", parse_engine="textin:1+fp1",
        )
        block_id += 1
    for i in range(8):
        _seed_block(
            conn, block_id=block_id, doc_id=300 + i, entity_id="CN.B",
            doc_type="quarterly", parse_engine="mock:1",
        )
        block_id += 1


@pytest.mark.db
def test_sampling_is_deterministic_for_the_same_seed(conn: psycopg.Connection) -> None:
    _seed_diverse_pool(conn)

    first = sample_candidates(conn, n=10, seed=7)
    second = sample_candidates(conn, n=10, seed=7)

    assert [c.block_id for c in first] == [c.block_id for c in second]


@pytest.mark.db
def test_different_seeds_can_pick_different_candidates(conn: psycopg.Connection) -> None:
    _seed_diverse_pool(conn)

    a = sample_candidates(conn, n=10, seed=1)
    b = sample_candidates(conn, n=10, seed=2)

    assert [c.block_id for c in a] != [c.block_id for c in b]


@pytest.mark.db
def test_every_present_stratum_gets_at_least_the_minimum(conn: psycopg.Connection) -> None:
    """H1：每层至少 5 条——除非那个层本身没那么多候选（如这里故意造的
    announcement 层只有 2 条）。"""
    _seed_diverse_pool(conn)

    picked = sample_candidates(conn, n=15, seed=3, min_per_stratum=5)

    by_stratum: dict[tuple[str, str], int] = {}
    for c in picked:
        by_stratum.setdefault((c.doc_type, c.entity_id or ""), 0)
        by_stratum[(c.doc_type, c.entity_id or "")] += 1

    assert by_stratum[("quarterly", "CN.A")] >= 5
    assert by_stratum[("quarterly", "CN.B")] >= 5
    assert by_stratum[("announcement", "CN.A")] == 2  # 池子里只有 2 条，有多少拿多少


@pytest.mark.db
def test_total_picked_reaches_n_when_pool_is_large_enough(conn: psycopg.Connection) -> None:
    _seed_diverse_pool(conn)

    picked = sample_candidates(conn, n=15, seed=3, min_per_stratum=5)

    assert len(picked) == 15


@pytest.mark.db
def test_engine_fingerprint_does_not_fragment_the_stratum(conn: psycopg.Connection) -> None:
    """分层键取引擎族（冒号前半段），不取带参数指纹的完整字符串——否则
    同一个 doc_type/entity 下换一次 xParse 参数版本就会被拆成新的一层。"""
    _seed_block(
        conn, block_id=1, doc_id=1, entity_id="CN.A", doc_type="quarterly",
        parse_engine="textin:1+aaa",
    )
    _seed_block(
        conn, block_id=2, doc_id=2, entity_id="CN.A", doc_type="quarterly",
        parse_engine="textin:2+bbb",
    )

    picked = sample_candidates(conn, n=10, seed=1, min_per_stratum=5)

    assert len(picked) == 2  # 同一层，两条都进来，没有因为指纹不同被当成两层各取一条


@pytest.mark.db
def test_superseded_documents_are_excluded_from_the_pool(conn: psycopg.Connection) -> None:
    _seed_block(
        conn, block_id=1, doc_id=1, entity_id="CN.A", doc_type="quarterly",
        parse_engine="textin:1+aaa",
    )
    conn.execute("UPDATE core.document SET superseded_at = now() WHERE doc_id = 1")
    conn.commit()

    picked = sample_candidates(conn, n=10, seed=1)

    assert picked == []


@pytest.mark.db
def test_write_candidates_csv_includes_block_id_for_gold_backfill(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _seed_diverse_pool(conn)
    picked = sample_candidates(conn, n=5, seed=1)
    out = tmp_path / "candidates.csv"

    write_candidates_csv(out, picked)

    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5
    assert {int(r["block_id"]) for r in rows} == {c.block_id for c in picked}
    assert "doc_type" in rows[0]
