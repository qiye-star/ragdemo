"""参数中的认证字段剥离。

provider_snapshot.params 会原样记录调用参数。某些供应商把 token 放在
query string 里，不剥离就等于把密钥写进数据库（docs/09-compliance-security.md §4.2）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

SECRET_PARAM_NAMES = frozenset(
    {
        "token", "api_key", "apikey", "access_token", "refresh_token",
        "secret", "client_secret", "password", "passwd", "pwd",
        "authorization", "auth", "sig", "signature", "credential",
    }
)


def _is_secret(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(name in lowered for name in SECRET_PARAM_NAMES)


def _strip_value(value: object) -> object:
    """对非 Mapping 的值做同样的递归：Mapping 原样剥离，list/tuple 逐个元素下潜，
    其余标量原样透传。"""
    if isinstance(value, Mapping):
        return strip_secrets(value)
    if isinstance(value, list | tuple):
        return type(value)(_strip_value(item) for item in value)
    return value


def strip_secrets(params: Mapping[str, Any]) -> dict[str, Any]:
    """返回一份剥离了认证字段的副本。输入不被修改。"""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if _is_secret(str(key)):
            out[key] = REDACTED
        else:
            out[key] = _strip_value(value)
    return out
