"""迁移 004：文档层与两个检索索引。

重点验证 docs/02-data-model.md §5.5 记录的那个陷阱不会复发：
entity_id 必须用 keyword 分词器（大小写敏感），用 raw 会静默匹配 0 行。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")


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
        conn.execute(
            "INSERT INTO core.document "
            "(doc_id, entity_id, doc_type, title, publish_at, source, content_hash, "
            " version_group_id, valid_from, known_at, ingest_run_id) "
            "OVERRIDING SYSTEM VALUE VALUES "
            "(1,'CN.688256','quarterly','三季报','2024-10-28 18:32+08','mock','h1',1,"
            " '2024-07-01','2024-10-28 18:32+08','r1')"
        )
        conn.execute(
            "INSERT INTO core.doc_block "
            "(doc_id, block_type, section_path, ordinal, page, content, is_leaf, "
            " entity_id, doc_type, publish_at, valid_from, known_at, source, ingest_run_id) "
            "VALUES (1,'paragraph','第三节 主营业务',1,12,"
            " '报告期内云端训练芯片出货量提升，智能计算收入同比增长 58.2%。',true,"
            " 'CN.688256','quarterly','2024-10-28 18:32+08','2024-07-01',"
            " '2024-10-28 18:32+08','mock','r1')"
        )
        conn.commit()
        yield conn


@pytest.mark.db
def test_both_retrieval_indexes_exist(db: psycopg.Connection[tuple[object, ...]]) -> None:
    defs = {
        str(r[0]): str(r[1])
        for r in db.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='core'"
        ).fetchall()
    }
    assert "USING bm25" in defs["doc_block_bm25"]
    assert "USING hnsw" in defs["doc_block_embedding_hnsw"]


@pytest.mark.db
def test_chinese_bm25_search_works(db: psycopg.Connection[tuple[object, ...]]) -> None:
    rows = db.execute(
        "SELECT block_id FROM core.doc_block WHERE content @@@ '云端训练芯片'"
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.db
def test_entity_id_term_is_case_sensitive(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """keyword 分词器：大写命中、小写不命中。用 raw 会正好相反，且不报错。"""
    upper = db.execute(
        "SELECT count(*) FROM core.doc_block "
        "WHERE block_id @@@ paradedb.term('entity_id','CN.688256')"
    ).fetchone()
    lower = db.execute(
        "SELECT count(*) FROM core.doc_block "
        "WHERE block_id @@@ paradedb.term('entity_id','cn.688256')"
    ).fetchone()
    assert upper is not None
    assert lower is not None
    assert (upper[0], lower[0]) == (1, 0)


@pytest.mark.db
def test_embedding_cache_key_includes_model_and_owner(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """同内容不同模型必须能共存，否则换模型后会读到旧向量。"""
    vec = "[" + ",".join(["0"] * 1024) + "]"
    ins = (
        "INSERT INTO core.embedding_cache (content_hash, model, owner_user, embedding) "
        "VALUES ('h', %s, %s, %s)"
    )
    with db.transaction(force_rollback=True):
        db.execute(ins, ("bge-m3", "", vec))
        db.execute(ins, ("qwen3-embedding", "", vec))
        db.execute(ins, ("bge-m3", "u1", vec))
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(ins, ("bge-m3", "", vec))
