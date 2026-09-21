"""检索测试的共享语料：两家公司、三份文档、含表格与历史时点。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.embed.batch import embed_pending_blocks
from ragdemo.embed.mock import MockEmbedder
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

_ENTITIES = (
    ("CN.688256", "寒武纪-U", "688256.SH"),
    ("CN.002049", "紫光国微", "002049.SZ"),
)

# (doc_id, entity_id, doc_type, title, publish_at, content_hash)
_DOCS = (
    (1, "CN.688256", "quarterly", "寒武纪 2024 年三季报", "2024-10-28 18:32+08", "h1"),
    (2, "CN.688256", "announcement", "关于签订重大销售合同的公告", "2025-03-05 19:00+08", "h2"),
    (3, "CN.002049", "quarterly", "紫光国微 2024 年三季报", "2024-10-25 17:10+08", "h3"),
)

# (doc_id, ordinal, block_type, section_path, content, is_leaf, parent_ordinal)
_BLOCKS = (
    (1, 0, "paragraph", "第三节 主营业务", "第三节 主营业务\n（父块）", False, None),
    (1, 1, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n报告期内云端训练芯片出货量提升，智能计算集群系统业务收入 12,340 万元，"
     "同比增长 58.2%。", True, 0),
    (1, 2, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n研发费用 1,890 万元，同比增长 22.4%，主要用于下一代训练芯片流片。",
     True, 0),
    (1, 3, "table", "第三节 主营业务",
     "| 业务分部 | 收入(万元) | 同比 |\n| 智能计算 | 12,340 | +58.2% |", True, None),
    (2, 0, "paragraph", "正文",
     "正文\n公司与某云计算厂商签订云端训练芯片采购合同，合同金额 8.5 亿元。", True, None),
    (3, 0, "paragraph", "第三节 主营业务",
     "第三节 主营业务\n特种集成电路业务收入 4,021 万元，智能安全芯片需求平稳。", True, None),
)


@pytest.fixture()
def corpus(temp_db: str) -> psycopg.Connection:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)

    for entity_id, name, code in _ENTITIES:
        conn.execute(
            "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
            " l2_segment, l3_node, primary_node, tushare_code) "
            "VALUES (%s,%s,'listed','算力','AI芯片',ARRAY['云端训练芯片'],"
            " '云端训练芯片',%s)",
            (entity_id, name, code),
        )

    for doc_id, entity_id, doc_type, title, publish_at, chash in _DOCS:
        conn.execute(
            "INSERT INTO core.document (doc_id, entity_id, doc_type, title, publish_at,"
            " source, content_hash, version_group_id, valid_from, known_at, ingest_run_id)"
            " OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,'mock',%s,%s,"
            " '2024-07-01',%s,'r1')",
            (doc_id, entity_id, doc_type, title, publish_at, chash, doc_id, publish_at),
        )

    parent_ids: dict[tuple[int, int], int] = {}
    for doc_id, ordinal, btype, section, content, is_leaf, parent_ordinal in _BLOCKS:
        parent_block_id = (
            parent_ids.get((doc_id, parent_ordinal)) if parent_ordinal is not None else None
        )
        row = conn.execute(
            "INSERT INTO core.doc_block (doc_id, parent_block_id, block_type, section_path,"
            " ordinal, page, content, is_leaf, entity_id, doc_type, publish_at,"
            " valid_from, known_at, source, ingest_run_id) "
            "SELECT %s,%s,%s,%s,%s,1,%s,%s,d.entity_id,d.doc_type,d.publish_at,"
            " d.valid_from,d.known_at,'mock','r1' FROM core.document d WHERE d.doc_id = %s "
            "RETURNING block_id",
            (doc_id, parent_block_id, btype, section, ordinal, content, is_leaf, doc_id),
        ).fetchone()
        assert row is not None
        parent_ids[(doc_id, ordinal)] = int(row[0])

    conn.commit()
    embed_pending_blocks(conn, MockEmbedder())
    conn.commit()
    return conn


@pytest.fixture()
def as_of_2024() -> datetime:
    """2024-12-31：能看到两份三季报，看不到 2025-03 的公告。"""
    return datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
