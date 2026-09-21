"""把 docs/06-retrieval.md §4.2 的检索 SQL 在真实引擎上跑一遍。

docs/10-roadmap.md P0 要求「本阶段同时验证本文档集的 SQL」，并且说
「pg_search 索引选项若与固定版本不符，以实测为准修正文档」。

实测结论（paradedb/paradedb:0.25.9-pg18）：**§4.2 的 SQL 按原文跑不起来。**
三处问题，本文件逐条钉死，文档已同步修正。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
AS_OF = "2025-01-01T00:00:00+00:00"
QVEC = "[" + ",".join(["0.03125"] * 1024) + "]"

_BM25_HEAD = """
  SELECT block_id,
         ROW_NUMBER() OVER (ORDER BY paradedb.score(block_id) DESC) AS rnk
    FROM asof.doc_block
   WHERE content @@@ %(query_text)s
     AND known_at <= %(as_of)s
     AND (superseded_at IS NULL OR superseded_at > %(as_of)s)
     AND is_leaf
     AND (%(entity_ids)s::text[] IS NULL OR entity_id = ANY (%(entity_ids)s::text[]))
     AND (%(doc_types)s::text[]  IS NULL OR doc_type  = ANY (%(doc_types)s::text[]))
"""
_BM25_TAIL = """
   ORDER BY paradedb.score(block_id) DESC
   LIMIT %(candidate_k)s
"""

# docs/06-retrieval.md §4.2 原文的 owner 谓词。
OWNER_DOCS = """
     AND owner_tenant IS NOT DISTINCT FROM %(tenant)s
     AND owner_user   IS NOT DISTINCT FROM %(user)s
"""
# 实测可用的等价写法。
OWNER_FIXED = """
     AND (owner_tenant = %(tenant)s OR (owner_tenant IS NULL AND %(tenant)s::text IS NULL))
     AND (owner_user   = %(user)s   OR (owner_user   IS NULL AND %(user)s::text   IS NULL))
"""

_VEC_HEAD = """
  SELECT block_id,
         ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rnk
    FROM asof.doc_block
   WHERE embedding IS NOT NULL
     AND known_at <= %(as_of)s
     AND (superseded_at IS NULL OR superseded_at > %(as_of)s)
     AND is_leaf
     AND (%(entity_ids)s::text[] IS NULL OR entity_id = ANY (%(entity_ids)s::text[]))
     AND (%(doc_types)s::text[]  IS NULL OR doc_type  = ANY (%(doc_types)s::text[]))
"""
_VEC_TAIL = """
   ORDER BY embedding <=> %(qvec)s::vector
   LIMIT %(candidate_k)s
"""


def _retrieval_sql(owner_clause: str) -> str:
    return f"""
WITH bm25 AS ({_BM25_HEAD}{owner_clause}{_BM25_TAIL}),
vec AS ({_VEC_HEAD}{owner_clause}{_VEC_TAIL})
SELECT block_id,
       COALESCE(%(w_bm25)s::numeric / (%(rrf_k)s::numeric + bm25.rnk), 0)
     + COALESCE(%(w_vec)s::numeric  / (%(rrf_k)s::numeric + vec.rnk),  0) AS rrf_score
  FROM bm25 FULL OUTER JOIN vec USING (block_id)
 ORDER BY rrf_score DESC
 LIMIT %(fusion_k)s
"""


PARAMS: dict[str, object] = {
    "query_text": "云端训练芯片",
    "as_of": AS_OF,
    "entity_ids": None,
    "doc_types": None,
    "tenant": None,
    "user": None,
    "candidate_k": 50,
    "fusion_k": 50,
    "qvec": QVEC,
    "w_bm25": 1,
    "w_vec": 1,
    "rrf_k": 60,
}


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.execute(
            "INSERT INTO core.entity "
            "(entity_id, name_full, entity_type, l1_layer, l2_segment, l3_node, primary_node) "
            "VALUES ('CN.688041','海光信息技术股份有限公司','listed','算力','AI芯片',"
            " ARRAY['通用服务器CPU'],'通用服务器CPU')"
        )
        conn.execute(
            "INSERT INTO core.document "
            "(doc_id, entity_id, doc_type, title, publish_at, source, content_hash,"
            " version_group_id, valid_from, known_at, ingest_run_id) "
            "OVERRIDING SYSTEM VALUE VALUES "
            "(1,'CN.688041','quarterly','三季报','2024-10-28 18:32+08','mock','h1',1,"
            " '2024-07-01','2024-10-28 18:32+08','r1')"
        )
        conn.execute(
            "INSERT INTO core.doc_block "
            "(doc_id, block_type, ordinal, content, embedding, is_leaf, entity_id, doc_type,"
            " publish_at, valid_from, known_at, source, ingest_run_id) VALUES "
            "(1,'paragraph',1,'报告期内云端训练芯片出货量提升，智能计算收入增长。',"
            " %s::vector,true,'CN.688041','quarterly','2024-10-28 18:32+08','2024-07-01',"
            " '2024-10-28 18:32+08','mock','r1'),"
            "(1,'paragraph',2,'公司治理与内部控制情况说明。',"
            " %s::vector,true,'CN.688041','quarterly','2024-10-28 18:32+08','2024-07-01',"
            " '2024-10-28 18:32+08','mock','r1')",
            (QVEC, QVEC),
        )
        conn.commit()
        yield conn


def _set_as_of(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    conn.execute("SELECT set_config('app.as_of', %s, true)", (AS_OF,))


@pytest.mark.db
def test_docs_owner_predicate_is_rejected_by_paradedb(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """坑 1：`IS NOT DISTINCT FROM` 与 `paradedb.score()` 同时出现，规划器直接拒绝。

    错误是 `Unsupported query shape`，不是返回错结果——好在它响亮。
    单独用 `IS NOT DISTINCT FROM`（不取 score）是可以的，
    所以只看文档、不实跑发现不了。
    """
    with db.transaction(force_rollback=True):
        _set_as_of(db)
        with pytest.raises(psycopg.errors.InternalError_, match="Unsupported query shape"):
            db.execute(_retrieval_sql(OWNER_DOCS), PARAMS).fetchall()


@pytest.mark.db
def test_fixed_retrieval_sql_runs_and_ranks(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """展开成 OR 形式后，§4.2 的整条检索链路（双路召回 + RRF）跑通。"""
    with db.transaction():
        _set_as_of(db)
        rows = db.execute(_retrieval_sql(OWNER_FIXED), PARAMS).fetchall()
    assert len(rows) == 2  # 向量路召回两块，BM25 路只命中一块
    assert rows[0][0] == 1  # 命中「云端训练芯片」的那块被 RRF 排到第一


@pytest.mark.db
def test_bm25_index_is_actually_used(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """BM25 一路真的走了索引，而不是退化成全表扫再过滤。"""
    with db.transaction():
        _set_as_of(db)
        plan = "\n".join(
            str(r[0])
            for r in db.execute(
                "EXPLAIN (ANALYZE, VERBOSE) " + _retrieval_sql(OWNER_FIXED), PARAMS
            ).fetchall()
        )
    assert "doc_block_bm25" in plan


@pytest.mark.db
def test_hnsw_session_gucs_from_docs_are_accepted(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """§4.2 开头那三个 hnsw.* 会话参数在本镜像上确实存在。"""
    with db.transaction():
        db.execute("SET LOCAL hnsw.ef_search = 200")
        db.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        db.execute("SET LOCAL hnsw.max_scan_tuples = 200000")
        row = db.execute("SELECT current_setting('hnsw.ef_search')").fetchone()
    assert row is not None
    assert row[0] == "200"


@pytest.mark.db
def test_set_local_rejects_a_placeholder(db: psycopg.Connection[tuple[object, ...]]) -> None:
    """坑 2：§4.2 写的是 `SET LOCAL app.as_of = :as_of`，而 SET 不接受占位符。

    按原文参数化执行会直接语法错。正确写法是 set_config()，
    ragdemo_core.db.session.as_of_session 就是这么做的。
    """
    with db.transaction(force_rollback=True), pytest.raises(psycopg.errors.SyntaxError):
        db.execute("SET LOCAL app.as_of = %s", (AS_OF,))


@pytest.mark.db
def test_empty_string_tenant_would_hide_every_public_block(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    """坑 3：公共读取时 owner 参数必须绑 NULL，绑空串会一条都召不回。

    而 as_of_session 把 app.tenant / app.user 设成空串。两边约定不一致时
    检索**静默返回空集**，没有任何报错——P1 的 RetrievalService 必须统一。
    """
    with db.transaction():
        _set_as_of(db)
        public = db.execute(_retrieval_sql(OWNER_FIXED), PARAMS).fetchall()
        blank = db.execute(
            _retrieval_sql(OWNER_FIXED), {**PARAMS, "tenant": "", "user": ""}
        ).fetchall()
    assert len(public) == 2
    assert blank == []
