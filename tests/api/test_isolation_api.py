"""GET /api/isolation/probes 与 /api/isolation/catalog 集成测试。

这组测试证明隔离机制确实生效，而不只是探针代码本身逻辑自洽——用真实
app_diag 登录用户（不是超级用户）+ 真实 RLS 策略 + 真实种子数据。
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from ragdemo.seed.isolation_demo import SYNTHETIC_MARK, seed_isolation_demo

AS_OF = "2025-06-01T00:00:00Z"


def _check_status(body: dict[str, object], name: str) -> str:
    checks = body["checks"]
    assert isinstance(checks, list)
    match = next(c for c in checks if c["name"] == name)
    status = match["status"]
    assert isinstance(status, str)
    return status


@pytest.mark.db
def test_matrix_has_four_identity_branches_and_two_error_branches(client: TestClient) -> None:
    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert {b["key"] for b in body["identity_branches"]} == {"public", "tenant", "user_a", "user_b"}
    assert {b["key"] for b in body["error_branches"]} == {"missing_as_of", "base_table_denied"}


@pytest.mark.db
def test_demo_not_seeded_marks_private_checks_as_skipped(client: TestClient) -> None:
    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert body["demo_seeded"] is False
    assert _check_status(body, "no_foreign_user_row") == "skipped"
    assert _check_status(body, "public_sees_no_private") == "skipped"
    # 这两条与 demo 是否存在无关，永远正常计算。
    assert _check_status(body, "monotonic_vs_public") in {"passed", "failed"}
    assert _check_status(body, "not_silently_empty") in {"passed", "failed"}


@pytest.mark.db
def test_missing_as_of_branch_reports_sqlstate_22023(client: TestClient) -> None:
    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    branch = next(b for b in r.json()["error_branches"] if b["key"] == "missing_as_of")
    assert branch["raised"] is True
    assert branch["sqlstate"] == "22023"
    assert branch["passed"] is True


@pytest.mark.db
def test_base_table_branch_reports_sqlstate_42501(client: TestClient) -> None:
    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    branch = next(b for b in r.json()["error_branches"] if b["key"] == "base_table_denied")
    assert branch["raised"] is True
    assert branch["sqlstate"] == "42501"
    assert branch["passed"] is True


@pytest.mark.db
def test_connection_usable_after_error_branches(client: TestClient) -> None:
    """六个分支跑完之后，同一次请求返回的其余数据必须完整——错误分支的
    异常必须被正确吸收，不会把连接拖进 InFailedSqlTransaction 而毒死
    后续分支。"""
    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert len(body["identity_branches"]) == 4
    for b in body["identity_branches"]:
        assert b["documents"]["total"] >= 0

    # 紧接着同一个 client 再发一条完全不同的路由，确认应用整体没有被拖垮。
    r2 = client.get("/api/quality/registry", params={"as_of": AS_OF})
    assert r2.status_code == 200


@pytest.mark.db
def test_identity_query_params_do_not_change_the_response(client: TestClient) -> None:
    baseline = client.get("/api/isolation/probes", params={"as_of": AS_OF}).json()
    with_params = client.get(
        "/api/isolation/probes",
        params={"as_of": AS_OF, "user": "someone-else", "tenant": "other-tenant"},
    ).json()
    assert with_params == baseline


@pytest.mark.db
def test_catalog_reports_force_rls_and_non_superuser_owner(client: TestClient) -> None:
    r = client.get("/api/isolation/catalog", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()

    by_rel = {row["relname"]: row for row in body["rls_status"]}
    assert by_rel["document"]["relrowsecurity"] is True
    assert by_rel["document"]["relforcerowsecurity"] is True
    assert by_rel["document"]["owner_is_superuser"] is False
    assert by_rel["doc_block"]["relrowsecurity"] is True
    assert by_rel["doc_block"]["relforcerowsecurity"] is True
    assert by_rel["doc_block"]["owner_is_superuser"] is False

    view_owners = {row["relname"]: row for row in body["asof_view_owners"]}
    assert view_owners["document"]["owner_is_superuser"] is False
    assert view_owners["doc_block"]["owner_is_superuser"] is False

    identity = body["connection_identity"]
    assert identity["current_user_is_superuser"] is False
    assert identity["asof_doc_block_select"] is True
    assert identity["core_doc_block_select"] is False
    assert identity["core_doc_block_insert"] is False

    assert body["as_of_affects_result"] is False


@pytest.mark.db
def test_catalog_policies_are_the_expected_six(client: TestClient) -> None:
    r = client.get("/api/isolation/catalog", params={"as_of": AS_OF})
    assert r.status_code == 200
    policies = {(p["tablename"], p["cmd"], p["policyname"]) for p in r.json()["policies"]}
    assert policies == {
        ("document", "SELECT", "doc_visibility"),
        ("document", "INSERT", "doc_insert"),
        ("document", "UPDATE", "doc_update"),
        ("doc_block", "SELECT", "block_visibility"),
        ("doc_block", "INSERT", "block_insert"),
        ("doc_block", "UPDATE", "block_update"),
    }


@pytest.mark.db
def test_user_a_sees_its_own_private_rows_after_seeding(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        seed_isolation_demo(conn, dsn=admin_dsn)

    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    body = r.json()
    assert body["demo_seeded"] is True

    by_key = {b["key"]: b for b in body["identity_branches"]}
    assert by_key["user_a"]["documents"]["user_private_rows"] == 1
    assert by_key["user_b"]["documents"]["user_private_rows"] == 1
    assert by_key["public"]["documents"]["user_private_rows"] == 0
    assert by_key["tenant"]["documents"]["user_private_rows"] == 0
    assert by_key["tenant"]["documents"]["tenant_private_rows"] == 1

    for name in (
        "no_foreign_user_row",
        "no_foreign_tenant_row",
        "public_sees_no_private",
        "tenant_sees_no_user_private",
        "owner_user_never_empty_string",
    ):
        assert _check_status(body, name) == "passed", name


@pytest.mark.db
def test_probe_response_contains_no_seeded_private_text(
    client: TestClient, api_db: tuple[str, str]
) -> None:
    """约束 6 的字面证明：即使种子数据存在，探针响应里也不出现任何
    合成文档的标题/正文字符串——只有聚合计数，一个文本列都不取。"""
    import psycopg

    admin_dsn, _ = api_db
    with psycopg.connect(admin_dsn) as conn:
        seed_isolation_demo(conn, dsn=admin_dsn)

    r = client.get("/api/isolation/probes", params={"as_of": AS_OF})
    assert r.status_code == 200
    assert SYNTHETIC_MARK not in r.text

    r2 = client.get("/api/isolation/catalog", params={"as_of": AS_OF})
    assert r2.status_code == 200
    assert SYNTHETIC_MARK not in r2.text
