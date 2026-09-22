"""行值转换：把 `Connection[tuple[object, ...]]` 里的裸 `object` 转成具体类型。

诊断接口的连接统一是 `Connection[tuple[object, ...]]`——`mypy --strict`
下每一列都是 `object`，不能直接喂给 `int()`/`str()`/`bool()`（它们的重载
签名都不接受裸 `object`）。这里用 `isinstance` 窄化后再转换，错的类型在
这里报错，而不是到处撒 `# type: ignore` 让它悄悄传播到业务逻辑里。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError(f"期望 int，收到 bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    raise TypeError(f"期望 int，收到 {type(value).__name__}: {value!r}")


def as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"期望 str，收到 {type(value).__name__}: {value!r}")
    return value


def as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return as_str(value)


def as_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"期望 bool，收到 {type(value).__name__}: {value!r}")
    return value


def as_float(value: object) -> float:
    # bool 是 int 的子类，isinstance(True, int) 为真——先排掉它，
    # 否则 True/False 会被当成 1.0/0.0 悄悄转换。
    if isinstance(value, bool):
        raise TypeError(f"期望 float，收到 bool: {value!r}")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"期望 float，收到 {type(value).__name__}: {value!r}")


def as_optional_float(value: object) -> float | None:
    if value is None:
        return None
    return as_float(value)


def as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"期望 datetime，收到 {type(value).__name__}: {value!r}")
    return value
