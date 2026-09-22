"""ADR-0010 约束的守门人：一遍遍历测试覆盖全部路由，而不是人工审查规矩。

不需要数据库：只检查 app.openapi() 的 schema 与路由声明本身，
不发出任何真正命中数据库的请求。
"""

from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from ragdemo.api.app import create_app
from ragdemo.api.constants import BANNER
from ragdemo.api.settings import ApiSettings

_SETTINGS = ApiSettings(
    dsn="postgresql://ragdemo_api@127.0.0.1:5433/ragdemo", set_role=None, web_root=None
)


def test_every_route_requires_as_of() -> None:
    app = create_app(_SETTINGS)
    schema = app.openapi()
    checked = 0
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            params = {
                (p["name"], p["in"], p.get("required")) for p in operation.get("parameters", [])
            }
            assert ("as_of", "query", True) in params, (
                f"{method.upper()} {path} 缺少必填 as_of 参数: {params}"
            )
            checked += 1
    assert checked > 0, "没有任何路由被检查到——测试本身可能失效了"


def test_no_route_exposes_a_write_method() -> None:
    app = create_app(_SETTINGS)
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods = route.methods or set()
            assert methods <= {"GET", "HEAD"}, f"{route.path} 暴露了写方法: {methods}"


def test_docs_ui_disabled_no_cdn_dependency() -> None:
    app = create_app(_SETTINGS)
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url == "/api/openapi.json"


def test_banner_matches_adr_text() -> None:
    assert BANNER == "内部诊断工具，非产品界面"


def test_no_cors_middleware_installed() -> None:
    app = create_app(_SETTINGS)
    middleware_classes = {getattr(m.cls, "__name__", str(m.cls)) for m in app.user_middleware}
    assert "CORSMiddleware" not in middleware_classes


def test_web_root_none_skips_static_mount() -> None:
    """settings.web_root=None（测试专用）不应该尝试挂载静态目录——否则
    每个契约测试都要一份真实的 web/ 目录才能装配 app，测试就绑死了前端。"""
    app = create_app(_SETTINGS)
    assert not any(isinstance(route, Mount) for route in app.routes)


def test_validation_runs_before_any_db_connection_is_attempted() -> None:
    """回归测试：get_conn 曾经在 as_of 完全缺失时依然被调用（两者是路由函数的
    平级依赖，FastAPI 各自独立求值，不会因为兄弟依赖校验失败而跳过）——
    实测过响应是 503 db_unavailable 而不是 422 as_of_required。deps.py 的
    get_conn 现在把 as_of 声明成自己的参数，强制它成为前置依赖。这里用一个
    连不上的假 DSN 验证：缺失 as_of 必须拿到 422，绝不能是 503——503 就说明
    数据库连接抢在校验之前被尝试了。"""
    app = create_app(
        ApiSettings(
            dsn="postgresql://unreachable-host-for-test:5433/x", set_role=None, web_root=None
        )
    )
    client = TestClient(app)

    r = client.get("/api/meta")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "as_of_required"

    r2 = client.get("/api/meta", params={"as_of": "not-a-date"})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "as_of_invalid"
