"""时点会话：把 as_of 绑定到一个事务上。

用 SET LOCAL 语义（set_config 的第三个参数为 true）而不是 SET：
时点随事务结束自动清除，连接池复用时不会把上一个请求的 as_of 泄漏给下一个。
SET LOCAL 本身不支持参数占位符，所以走 set_config()。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg


class NaiveDatetimeError(ValueError):
    """as_of 必须带时区。"""


@contextmanager
def as_of_session(
    conn: psycopg.Connection[tuple[object, ...]],
    as_of: datetime,
    *,
    tenant: str | None = None,
    user: str | None = None,
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """开启一个锁定时点的事务。

    Args:
        conn: 数据库连接。
        as_of: 查询假设的时刻，**必须带时区**，无默认值。
        tenant: 租户标识，供 RLS 使用；None 表示只看公共数据。
        user: 用户标识，供 RLS 使用；None 表示只看公共数据。

    Yields:
        同一个连接，但此刻它的事务里已绑定时点。

    Raises:
        NaiveDatetimeError: as_of 不带时区。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise NaiveDatetimeError(f"as_of 必须带时区，收到 naive datetime: {as_of!r}")

    with conn.transaction():
        conn.execute("SELECT set_config('app.as_of', %s, true)", (as_of.isoformat(),))
        conn.execute("SELECT set_config('app.tenant', %s, true)", (tenant or "",))
        conn.execute("SELECT set_config('app.user', %s, true)", (user or "",))
        yield conn
