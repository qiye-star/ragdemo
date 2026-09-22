"""GET /api/meta 集成测试：真实数据库 + app_diag 登录用户 + TestClient。"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient


@pytest.mark.db
def test_meta_reports_non_superuser_identity_and_visible_counts(client: TestClient) -> None:
    r = client.get("/api/meta", params={"as_of": "2030-01-01T00:00:00Z"})
    assert r.status_code == 200
    body = r.json()
    assert body["banner"] == "内部诊断工具，非产品界面"
    assert body["read_only"] is True
    # app_diag 不是超级用户——这是「只查 asof 视图」机械保证成立的前提。
    assert body["db"]["current_user_is_superuser"] is False
    assert isinstance(body["visible"]["documents"], int)
    assert isinstance(body["visible"]["blocks"], int)


@pytest.mark.db
def test_meta_missing_as_of_returns_422_even_against_a_real_database(
    client: TestClient,
) -> None:
    r = client.get("/api/meta")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "as_of_required"


@pytest.mark.db
def test_meta_reports_null_known_at_range_when_no_document_is_visible(
    client: TestClient,
) -> None:
    # 空库（或当前 as_of 下一篇公开文档都不可见）时，前端需要能区分
    # 「还没有任何数据」和「有数据但没解析出范围」——两个字段都必须是
    # None，而不是缺省成 0 或空字符串误导前端画出一条假的时间轴。
    r = client.get("/api/meta", params={"as_of": "2030-01-01T00:00:00Z"})
    assert r.status_code == 200
    assert r.json()["known_at_range"] == {"earliest": None, "latest": None}


@pytest.mark.db
def test_meta_reports_known_at_range_across_public_documents(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    """as_of 选择器的核心用户痛点：不选定一个时点就看不到任何东西，
    但不看到东西又不知道该选哪个时点。/api/meta 必须报出「当前 as_of 下，
    公开文档 known_at 最早/最晚是什么时候」，前端才能把这个范围画成
    可点击的引导（而不是让用户对着一个空 datetime-local 输入框瞎猜）。
    """
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        for title, known_at, content_hash in (
            ("早期公开文档", "2021-03-01T00:00:00+00", "h-earliest"),
            ("居中公开文档", "2023-01-01T00:00:00+00", "h-middle"),
            ("最晚公开文档", "2024-09-01T00:00:00+00", "h-latest"),
        ):
            conn.execute(
                "INSERT INTO core.document"
                " (doc_type, title, publish_at, source, content_hash, version_group_id,"
                "  page_count, owner_user, valid_from, known_at, ingest_run_id)"
                " VALUES ('quarterly', %s, %s, 'mock', %s, 0, 1, NULL, %s, %s, 'r1')",
                (title, known_at, content_hash, known_at, known_at),
            )
        conn.execute(
            "UPDATE core.document SET version_group_id = doc_id"
            " WHERE content_hash IN ('h-earliest', 'h-middle', 'h-latest')"
        )
        conn.commit()

    r = client.get("/api/meta", params={"as_of": "2030-01-01T00:00:00Z"})
    assert r.status_code == 200
    rng = r.json()["known_at_range"]
    assert rng["earliest"].startswith("2021-03-01")
    assert rng["latest"].startswith("2024-09-01")

    # as_of 早于「居中公开文档」——它此刻还不可见，范围应该只到最早那篇，
    # 证明这不是全表扫描出来的静态范围，而是真的按当前 as_of 收窄的。
    r2 = client.get("/api/meta", params={"as_of": "2021-06-01T00:00:00Z"})
    rng2 = r2.json()["known_at_range"]
    assert rng2["earliest"].startswith("2021-03-01")
    assert rng2["latest"].startswith("2021-03-01")
