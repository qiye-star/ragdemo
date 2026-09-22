"""GET /api/meta 集成测试：真实数据库 + app_diag 登录用户 + TestClient。"""

from __future__ import annotations

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
