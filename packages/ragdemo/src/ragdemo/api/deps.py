"""共享依赖：as_of 校验、连接获取。

约束 4（强制 as_of，无默认值）与约束 5（只查 asof 视图）都要求校验先于
任何数据库调用发生——`require_as_of` 是一个不接触连接的纯函数依赖，
`get_conn` 之后才建连接，两者互相独立，FastAPI 的依赖求值顺序保证前者的
异常会在后者执行前抛出。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import Depends, Query, Request
from psycopg import sql

from ragdemo.api.errors import ApiError, ErrorCode
from ragdemo.api.settings import ApiSettings

# `as_of=2026-09-22T00:00:00+08:00` 经 query string 传输时，`+` 是空格的
# 转义字符——不显式 %2B 编码就会被解码成空格。命中这个形状时，
# fromisoformat 会直接 ValueError，错误信息完全看不出所以然，
# 所以在解析之前先识别这个具体形状，给出点名 %2B 的提示。
_SPACE_OFFSET = re.compile(r"^.*T\d{2}:\d{2}(:\d{2}(\.\d+)?)? \d{2}:\d{2}$")


def require_as_of(
    as_of: Annotated[str, Query(description="必填，无默认值；带时区，如 2026-09-22T00:00:00Z")],
) -> datetime:
    """把 as_of 的三种失败形态分开报，而不是让它们全部撞成同一个 422。

    整个参数缺失（query string 里完全没有 as_of）由 FastAPI 自身的
    校验先一步拦下（这个依赖函数的签名没有默认值），对应的错误信封
    由 app.py 里的 RequestValidationError 处理器统一翻译成
    `ErrorCode.AS_OF_REQUIRED`——这里只处理"传了但值有问题"的情形。
    """
    raw = as_of.strip()
    if not raw:
        raise ApiError(422, ErrorCode.AS_OF_REQUIRED, "as_of 不能是空字符串", param="as_of")
    if _SPACE_OFFSET.match(raw):
        raise ApiError(
            400,
            ErrorCode.AS_OF_PLUS_DECODED_AS_SPACE,
            f"as_of 解析失败：{raw!r} 里的 '+' 时区偏移在 query string 里被解码成了空格",
            param="as_of",
            hint="发送 UTC 的 Z 形式（如 2026-09-22T00:00:00Z），或把 + 转义成 %2B",
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            422, ErrorCode.AS_OF_INVALID, f"as_of 无法解析为日期时间: {raw!r}", param="as_of"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(400, ErrorCode.AS_OF_NAIVE, "as_of 必须带时区", param="as_of")
    return parsed


AsOf = Annotated[datetime, Depends(require_as_of)]


def get_settings(request: Request) -> ApiSettings:
    settings = request.app.state.settings
    if not isinstance(settings, ApiSettings):
        raise RuntimeError("app.state.settings 未初始化为 ApiSettings——create_app 装配有误")
    return settings


SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


def get_conn(
    settings: SettingsDep,
    as_of: AsOf,
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """每请求一个连接。`autocommit=True` + 三个连接级 GUC：

    签名里的 `as_of` 参数不在函数体内使用，只是为了让 `get_conn` 显式
    依赖 `require_as_of`——两者若是路由函数的两个平级依赖（都直接声明在
    路由签名上），FastAPI 会各自独立求值，`as_of` 校验失败并不会阻止
    `get_conn` 的连接逻辑照常执行（实测验证过：完全不传 as_of 时，
    响应曾经是 503 db_unavailable 而不是 422 as_of_required——数据库
    连接抢先在校验失败之前就被尝试了）。把 `as_of` 声明成 `get_conn`
    自身的参数，强制它成为 `get_conn` 的前置依赖：FastAPI 对某个依赖
    的调用一定发生在它自己声明的子依赖全部成功解析之后。

    - `timezone=UTC`：timestamptz 回到 Python 时统一是 UTC aware，JSON
      输出不随服务器 TimeZone 漂移。
    - `statement_timeout=5000`：诊断接口的一个手滑查询不该拖住整个进程。
    - `default_transaction_read_only=on`：即使开发模式（`set_role`）忘了
      `SET ROLE`，写操作也会在数据库层面被拒——这是约束 1「无写接口」
      在连接层面的最后一道保险，代码本身无从绕过。
    """
    try:
        conn = psycopg.connect(
            settings.dsn,
            autocommit=True,
            options=(
                "-c timezone=UTC -c statement_timeout=5000 -c default_transaction_read_only=on"
            ),
        )
    except psycopg.OperationalError as exc:
        raise ApiError(503, ErrorCode.DB_UNAVAILABLE, f"无法连接数据库: {exc}") from exc

    try:
        if settings.set_role is not None:
            # 开发模式退路（settings.py 的模块 docstring）：超级用户连接上
            # 之后立刻收窄成 app_diag 身份，current_user 的权限判定从此
            # 生效——但 SET ROLE 可以被 RESET ROLE 撤销，这不是部署形态。
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(settings.set_role)))
        yield conn
    finally:
        conn.close()


ConnDep = Annotated[psycopg.Connection[tuple[object, ...]], Depends(get_conn)]
