"""FastAPI 应用装配：路由 / 异常处理器 / 静态挂载。不含 SQL。

`api/` 要能被整体删掉而不留痕迹（ADR-0010 推翻条件「维护它的时间超过
对应阶段总工时的 10%」的对策），这个文件因此只做装配：把 queries/*
的结果通过 routers/* 接到 HTTP 上，异常翻译成统一信封，静态资源挂载
在最后（否则会吃掉所有未匹配的 API 路径）。
"""

from __future__ import annotations

import mimetypes

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ragdemo.api.constants import BANNER
from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.routers import meta
from ragdemo.api.settings import ApiSettings

# Windows 上 mimetypes 靠读注册表判定扩展名，.js 在某些机器上会被映射成
# text/plain——<script type="module"> 的严格 MIME 检查会直接拒绝加载，
# 页面白屏且不报任何看得见的错误。本机实测是 application/javascript
# （不受影响），仍显式声明一遍，作为跨机器部署时的廉价保险。
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_app(settings: ApiSettings) -> FastAPI:
    app = FastAPI(
        title="ragdemo 内部诊断接口",
        description=BANNER,
        # Swagger UI 的 HTML 从 jsdelivr CDN 拉 JS，内网打不开，留着只是个
        # 404 陷阱；同理不装 CORSMiddleware——同源静态页不需要它，装了就是
        # 给「被拿去给外人看」开路（ADR-0010 推翻条件 2）。
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    app.include_router(meta.router, prefix="/api")

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.error_detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 完全没传 as_of 时，FastAPI 自身的必填校验先一步拦下（deps.py::
        # require_as_of 的签名没有默认值）——这里把它翻回统一信封，
        # 而不是让调用方看到 FastAPI 默认的 {"detail": [...]} 形状。
        for err in exc.errors():
            if "as_of" in err.get("loc", ()):
                payload = {
                    "error": {
                        "code": ErrorCode.AS_OF_REQUIRED,
                        "message": "as_of 未传，这个接口的每个请求都必须带 as_of",
                        "param": "as_of",
                    }
                }
                return JSONResponse(status_code=422, content=payload)
        payload = {"error": {"code": "validation_error", "message": str(exc.errors())}}
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(psycopg.errors.InvalidParameterValue)
    async def _asof_guc_missing_handler(
        request: Request, exc: psycopg.errors.InvalidParameterValue
    ) -> JSONResponse:
        # 22023 逃出 as_of_session 意味着某条查询绕过了它、直接查了 asof
        # 视图——约束 4 被绕过了。这是运行时告警器，不是普通业务错误。
        payload = {
            "error": {
                "code": ErrorCode.ASOF_GUC_MISSING,
                "message": str(exc),
                "sqlstate": exc.sqlstate,
            }
        }
        return JSONResponse(status_code=500, content=payload)

    @app.exception_handler(psycopg.errors.InsufficientPrivilege)
    async def _base_table_denied_handler(
        request: Request, exc: psycopg.errors.InsufficientPrivilege
    ) -> JSONResponse:
        # 42501 逃出到这里意味着某条查询碰了 core.* 时点基表——约束 5
        # 被绕过了。同样是运行时告警器。
        payload = {
            "error": {
                "code": ErrorCode.BASE_TABLE_DENIED,
                "message": str(exc),
                "sqlstate": exc.sqlstate,
            }
        }
        return JSONResponse(status_code=500, content=payload)

    # 静态挂载必须放最后：StaticFiles(html=True) 会吃掉所有未匹配的路径。
    # settings.web_root 为 None（测试用）或目录不存在时跳过，测试不依赖
    # web/ 的实际内容。
    if settings.web_root is not None and settings.web_root.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.web_root), html=True), name="web")

    return app
