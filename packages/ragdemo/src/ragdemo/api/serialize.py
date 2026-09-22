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


def as_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return as_datetime(value)


def as_bbox(value: object) -> tuple[tuple[float, float, float, float] | None, bool]:
    """返回 `(bbox 或 None, malformed)`。

    `numeric[4]` 这个类型声明在 PostgreSQL 里不校验数组长度——一行手写的
    坏数据完全可能塞进 3 个或 5 个元素。真实长度不是 4 时标记
    `malformed=True` 而不是静默截断或直接抛异常：这正是诊断工具应该
    暴露给人看的东西，不该在序列化这一层就把异常吞掉。
    """
    if value is None:
        return None, False
    if not isinstance(value, list | tuple):
        raise TypeError(f"期望 bbox 是数组，收到 {type(value).__name__}: {value!r}")
    if len(value) != 4:
        return None, True
    try:
        x0, y0, x1, y1 = (as_float(v) for v in value)
    except TypeError:
        return None, True
    return (x0, y0, x1, y1), False


def as_str_list(value: object) -> list[str]:
    """psycopg 把 jsonb 数组（如 `parse_warnings`）反序列化成
    `list[Any]`——对 mypy strict 而言仍是裸 `object`，逐项窄化成 `str`。
    `None` 映射成空列表：`parse_warnings` 列本身有 `NOT NULL DEFAULT '[]'`，
    但这里仍防御性处理，不假设调用方永远传非 None。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"期望 list，收到 {type(value).__name__}: {value!r}")
    return [as_str(item) for item in value]
