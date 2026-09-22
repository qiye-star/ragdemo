"""GET /api/documents 系集成测试：真实数据库 + app_diag 登录用户 + TestClient。

种子数据模式仿 tests/db/test_asof_layer.py::_seed_two_documents——一份公共
文档（带父块/叶子/一个无父表格叶子）、一份 u1 的私有文档，用来验证约束 6
（owner_user IS NOT NULL 的块硬编码不可见）在完整的 HTTP 路由链路上也生效，
不只是在裸 SQL 层面生效。
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

AS_OF = "2025-06-01T00:00:00Z"
PRIVATE_TITLE_MARK = "u1的私有测试材料标题"
PRIVATE_CONTENT_MARK = "这是私有内容不应该出现在任何响应里"


def _seed(admin_dsn: str) -> dict[str, int]:
    with psycopg.connect(admin_dsn) as conn:
        (public_doc_id,) = conn.execute(
            "INSERT INTO core.document"
            " (doc_type, title, publish_at, source, content_hash, version_group_id,"
            "  page_count, owner_user, valid_from, known_at, ingest_run_id)"
            " VALUES ('quarterly', '寒武纪测试季报', '2024-10-28T18:32:00+08', 'mock',"
            "         'h-public-test', 0, 2, NULL, '2024-10-28', '2024-10-28T18:32:00+08', 'r1')"
            " RETURNING doc_id"
        ).fetchone()  # type: ignore[misc]
        conn.execute(
            "UPDATE core.document SET version_group_id = doc_id WHERE doc_id = %s",
            (public_doc_id,),
        )

        (parent_id,) = conn.execute(
            "INSERT INTO core.doc_block"
            " (doc_id, block_type, section_path, ordinal, page, is_leaf, content,"
            "  doc_type, publish_at, valid_from, known_at, source, ingest_run_id)"
            " VALUES (%s, 'paragraph', '第一节', 0, 1, false, '',"
            "         'quarterly', '2024-10-28T18:32:00+08', '2024-10-28',"
            "         '2024-10-28T18:32:00+08', 'mock', 'r1') RETURNING block_id",
            (public_doc_id,),
        ).fetchone()  # type: ignore[misc]

        conn.execute(
            "INSERT INTO core.doc_block"
            " (doc_id, parent_block_id, block_type, section_path, ordinal, page, bbox,"
            "  is_leaf, char_len, content, doc_type, publish_at, valid_from, known_at,"
            "  source, ingest_run_id)"
            " VALUES (%s, %s, 'paragraph', '第一节', 1, 1,"
            "         ARRAY[0.1,0.2,0.3,0.4]::numeric[], true, 320, '公开叶子内容',"
            "         'quarterly', '2024-10-28T18:32:00+08', '2024-10-28',"
            "         '2024-10-28T18:32:00+08', 'mock', 'r1')",
            (public_doc_id, parent_id),
        )
        # 无父表格叶子——tree.py 的设计本身，parent_block_id IS NULL 且 is_leaf。
        conn.execute(
            "INSERT INTO core.doc_block"
            " (doc_id, block_type, section_path, ordinal, page, is_leaf, char_len,"
            "  content, table_html, doc_type, publish_at, valid_from, known_at,"
            "  source, ingest_run_id)"
            " VALUES (%s, 'table', '第一节', 2, 2, true, 900, '表格内容',"
            "         '<table></table>', 'quarterly', '2024-10-28T18:32:00+08',"
            "         '2024-10-28', '2024-10-28T18:32:00+08', 'mock', 'r1')",
            (public_doc_id,),
        )

        (private_doc_id,) = conn.execute(
            "INSERT INTO core.document"
            " (doc_type, title, publish_at, source, content_hash, version_group_id,"
            "  owner_user, valid_from, known_at, ingest_run_id)"
            f" VALUES ('user_upload', '{PRIVATE_TITLE_MARK}', '2024-10-28T18:32:00+08',"
            "         'mock', 'h-private-test', 0, 'u1', '2024-10-28',"
            "         '2024-10-28T18:32:00+08', 'r1') RETURNING doc_id"
        ).fetchone()  # type: ignore[misc]
        conn.execute(
            "UPDATE core.document SET version_group_id = doc_id WHERE doc_id = %s",
            (private_doc_id,),
        )
        (private_block_id,) = conn.execute(
            "INSERT INTO core.doc_block"
            " (doc_id, block_type, section_path, ordinal, is_leaf, content, owner_user,"
            "  doc_type, publish_at, valid_from, known_at, source, ingest_run_id)"
            f" VALUES (%s, 'paragraph', '', 0, true, '{PRIVATE_CONTENT_MARK}', 'u1',"
            "         'user_upload', '2024-10-28T18:32:00+08', '2024-10-28',"
            "         '2024-10-28T18:32:00+08', 'mock', 'r1') RETURNING block_id",
            (private_doc_id,),
        ).fetchone()  # type: ignore[misc]
        conn.commit()

    return {
        "public_doc_id": public_doc_id,
        "parent_block_id": parent_id,
        "private_doc_id": private_doc_id,
        "private_block_id": private_block_id,
    }


@pytest.fixture
def seeded(api_db: tuple[str, str]) -> dict[str, int]:
    admin_dsn, _ = api_db
    return _seed(admin_dsn)


@pytest.mark.db
def test_documents_route_returns_only_public_documents(
    client: TestClient, seeded: dict[str, int]
) -> None:
    r = client.get("/api/documents", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    doc_ids = {d["doc_id"] for d in body["documents"]}
    assert seeded["public_doc_id"] in doc_ids
    assert seeded["private_doc_id"] not in doc_ids
    assert PRIVATE_TITLE_MARK not in r.text


@pytest.mark.db
def test_document_detail_404_for_private_document(
    client: TestClient, seeded: dict[str, int]
) -> None:
    r = client.get(f"/api/documents/{seeded['private_doc_id']}", params={"as_of": AS_OF})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert PRIVATE_TITLE_MARK not in r.text


@pytest.mark.db
def test_document_detail_404_for_unknown_doc_id(client: TestClient) -> None:
    r = client.get("/api/documents/999999999", params={"as_of": AS_OF})
    assert r.status_code == 404


@pytest.mark.db
def test_document_detail_reports_block_type_and_page_counts(
    client: TestClient, seeded: dict[str, int]
) -> None:
    r = client.get(f"/api/documents/{seeded['public_doc_id']}", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert body["block_count"] == 3
    assert body["leaf_count"] == 2
    type_counts = {c["block_type"]: c for c in body["block_type_counts"]}
    assert type_counts["table"]["blocks"] == 1
    assert type_counts["paragraph"]["blocks"] == 2


@pytest.mark.db
def test_block_route_404_for_private_block(client: TestClient, seeded: dict[str, int]) -> None:
    r = client.get(f"/api/blocks/{seeded['private_block_id']}", params={"as_of": AS_OF})
    assert r.status_code == 404
    assert PRIVATE_CONTENT_MARK not in r.text


@pytest.mark.db
def test_page_layout_returns_normalized_bbox_and_counts_missing(
    client: TestClient, seeded: dict[str, int]
) -> None:
    r = client.get(
        f"/api/documents/{seeded['public_doc_id']}/pages/1/layout", params={"as_of": AS_OF}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bbox_normalized"] is True
    # page=1 有两个块：父块（无 bbox）+ 一个带 bbox 的叶子。
    assert body["bbox_missing_count"] == 1
    with_bbox = [b for b in body["blocks"] if b["bbox"] is not None]
    assert len(with_bbox) == 1
    assert with_bbox[0]["bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4])


@pytest.mark.db
def test_page_layout_404_for_unknown_document(client: TestClient) -> None:
    r = client.get("/api/documents/999999999/pages/1/layout", params={"as_of": AS_OF})
    assert r.status_code == 404


@pytest.mark.db
def test_tree_route_reports_orphan_leaves(client: TestClient, seeded: dict[str, int]) -> None:
    r = client.get(f"/api/documents/{seeded['public_doc_id']}/tree", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert body["node_count"] == 3
    assert body["orphan_leaf_count"] == 1
    assert body["cycle_detected"] is False
    assert len(body["roots"]) == 2  # 章节父块 root + 无父表格叶子 root


@pytest.mark.db
def test_blocks_route_flat_list_excludes_private_blocks(
    client: TestClient, seeded: dict[str, int]
) -> None:
    r = client.get(f"/api/documents/{seeded['public_doc_id']}/blocks", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert len(body["blocks"]) == 3
    assert PRIVATE_CONTENT_MARK not in r.text


@pytest.mark.db
def test_as_of_before_publish_returns_empty_document_list(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """空集 vs 报错的区分：早于文档发布时刻的 as_of 应该拿到 HTTP 200
    的空列表，而不是任何错误——这是"这个时点这份文档还不存在"的正常状态。
    """
    early = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    r = client.get("/api/documents", params={"as_of": early})
    assert r.status_code == 200
    assert r.json()["documents"] == []
