"""诊断接口的统一错误信封与错误码常量。

约束 4（强制 as_of）与约束 5（只查 asof 视图）不是靠代码评审守住的，是靠
运行时错误码：22023（GUC 未设）与 42501（越权查基表）逃出 as_of_session /
app_diag 的权限边界时，本模块把它们翻成 500 + 明确的 code，而不是让它们
被通用异常处理器吞成一个无法追查来源的 500。这两个 code 出现在响应里
本身就是一次「本该由数据库拦住的约束被绕过了」的告警。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ErrorCode:
    """错误信封里 error.code 的取值。用类命名空间而非 Enum：这些值只用作
    字符串字面量比较（HTTP 响应体、测试断言），Enum 的额外机制用不上。"""

    AS_OF_REQUIRED = "as_of_required"
    AS_OF_INVALID = "as_of_invalid"
    AS_OF_NAIVE = "as_of_naive"
    AS_OF_PLUS_DECODED_AS_SPACE = "as_of_plus_decoded_as_space"
    NOT_FOUND = "not_found"
    DB_UNAVAILABLE = "db_unavailable"
    # 以下两个是运行时告警器，不是正常业务错误——出现即代表约束 4/5 被绕过。
    ASOF_GUC_MISSING = "asof_guc_missing"
    BASE_TABLE_DENIED = "base_table_denied"


class ApiError(HTTPException):
    """统一错误信封：`{"error": {"code", "message", "param"?, "hint"?, "sqlstate"?}}`。

    继承 HTTPException 而不是自定义异常类型，是为了让 FastAPI/Starlette
    的默认异常传播路径原样生效（路由函数里 raise 即可，不需要额外注册这一个
    类型的 handler）；信封格式由 detail 的结构承载。

    `error_detail` 是这个结构化 payload 的显式类型化入口：Starlette 基类把
    `self.detail` 标注成 `str | None`（即使 FastAPI 的子类签名放宽成
    `Any`，mypy 按属性首次赋值处的类型解析，不看子类签名），运行时
    `self.detail` 确实就是下面这个 dict——FastAPI 序列化响应体时读的正是
    它——但要在类型检查下访问其字段，走 `error_detail` 而不是 `detail`。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        param: str | None = None,
        hint: str | None = None,
        sqlstate: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {"code": code, "message": message}
        if param is not None:
            detail["param"] = param
        if hint is not None:
            detail["hint"] = hint
        if sqlstate is not None:
            detail["sqlstate"] = sqlstate
        payload: dict[str, Any] = {"error": detail}
        super().__init__(status_code=status_code, detail=payload)
        self.error_detail: dict[str, Any] = payload
