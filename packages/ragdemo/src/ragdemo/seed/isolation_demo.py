"""权限隔离演示种子：合成的公共/租户私有/用户私有文档，供诊断界面的
隔离探针矩阵（`api/queries/isolation.py`）有真实数据可看。

**为什么不用 `ingest.documents.DocumentWriter`**：它的 `write_document`
只接受 `owner_user`，不接受 `owner_tenant`（`documents.py` 的模块内注释
原话：`owner_tenant` 需要的是 P4 才该做的东西）。业务写入路径没有
`owner_tenant` 这个能力不是这里要补的缺口——诊断种子是特例，直接手写
INSERT，不代表业务层多了一条能写租户私有文档的路。

**为什么 `publish_at = known_at = 2020-01-01`**：Dagster 的日分区从
`2022-01-01` 开始（`ingest/assets.py::DAILY`），2020 年的数据永远不会落进
任何一次分区物化的扫描窗口——`vector_coverage_check` 这类 blocking 检查
按 `publish_at` 圈定分区窗口，会把这些叶子块的 `embedding IS NULL`
（种子从不嵌入）算成覆盖率下降，进而挡住当天的下游物化。选一个分区起点
之前的日期，让这批合成数据在结构上就不参与任何真实检查。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

DEMO_SOURCE = "isolation-demo"
DEMO_TENANT = "diag-demo-tenant"
DEMO_USER_A = "diag-demo-user-a"
DEMO_USER_B = "diag-demo-user-b"

# 每一份合成文档的标题/正文都带这个标记——db 测试与隔离探针的响应体都要
# 断言它不出现在任何"本该看不见"的地方；同时它也提醒任何直接查库的人
# 这不是真实用户材料。
SYNTHETIC_MARK = "【合成占位文本 · 非真实材料 · isolation-demo】"

_DEMO_KNOWN_AT = datetime(2020, 1, 1, tzinfo=UTC)
_INGEST_RUN_ID = "isolation-demo-seed"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", None})


class AlreadySeeded(RuntimeError):
    """`core.document` 里已经有 `source = 'isolation-demo'` 的行。"""


class NotLoopbackHost(RuntimeError):
    """拒绝向非回环地址的数据库写入合成的"用户私有材料"演示数据。"""


@dataclass(frozen=True)
class _DemoDoc:
    key: str  # public / tenant / user_a / user_b
    title: str
    owner_tenant: str | None
    owner_user: str | None


_DEMO_DOCS: tuple[_DemoDoc, ...] = (
    _DemoDoc("public", f"{SYNTHETIC_MARK} 公共对照文档", None, None),
    _DemoDoc("tenant", f"{SYNTHETIC_MARK} 租户私有文档", DEMO_TENANT, None),
    _DemoDoc("user_a", f"{SYNTHETIC_MARK} 用户 A 私有文档", DEMO_TENANT, DEMO_USER_A),
    _DemoDoc("user_b", f"{SYNTHETIC_MARK} 用户 B 私有文档", DEMO_TENANT, DEMO_USER_B),
)


def _require_loopback(dsn: str) -> None:
    host = psycopg.conninfo.conninfo_to_dict(dsn).get("host")
    if host not in _LOOPBACK_HOSTS:
        raise NotLoopbackHost(
            f"拒绝对非回环地址 {host!r} 执行隔离演示种子——这会把合成的"
            "「用户私有材料」写进一个可能是共享/生产的数据库"
        )


def _content_hash(key: str) -> str:
    return hashlib.sha256(f"isolation-demo:{key}".encode()).hexdigest()


def seed_isolation_demo(
    conn: psycopg.Connection[tuple[object, ...]], *, dsn: str, force: bool = False
) -> dict[str, int]:
    """插入 4 份合成文档（公共对照 + 租户私有 + 用户 A/B 私有），每份一个块。

    返回 `{key: doc_id}`，key 取值见 `_DemoDoc.key`。`force=True` 时先调用
    `clear_isolation_demo` 清空已有的演示数据，再重新写入——幂等地"重置"，
    不是"追加"。
    """
    _require_loopback(dsn)

    (existing,) = conn.execute(
        "SELECT count(*) FROM core.document WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    if existing:
        if not force:
            raise AlreadySeeded(
                f"core.document 里已有 {existing} 行 source='{DEMO_SOURCE}'——"
                "先跑 `ragdemo db clear-isolation-demo --yes`，或加 --force"
            )
        clear_isolation_demo(conn, dsn=dsn)

    conn.execute(
        "INSERT INTO core.source_registry"
        " (source_id, vendor, layer, can_cache, can_show_raw, can_vectorize, time_precision)"
        " VALUES (%s, 'isolation-demo', 'user', false, false, false, 'second')"
        " ON CONFLICT (source_id) DO NOTHING",
        (DEMO_SOURCE,),
    )

    doc_ids: dict[str, int] = {}
    for demo in _DEMO_DOCS:
        row = conn.execute(
            "INSERT INTO core.document"
            " (doc_type, title, publish_at, source, content_hash, version_group_id,"
            "  owner_tenant, owner_user, valid_from, known_at, source_ref, ingest_run_id)"
            " VALUES ('user_upload', %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)"
            " RETURNING doc_id",
            (
                demo.title,
                _DEMO_KNOWN_AT,
                DEMO_SOURCE,
                _content_hash(demo.key),
                demo.owner_tenant,
                demo.owner_user,
                _DEMO_KNOWN_AT.date(),
                _DEMO_KNOWN_AT,
                f"isolation-demo:{demo.key}",
                _INGEST_RUN_ID,
            ),
        ).fetchone()
        assert row is not None
        assert isinstance(row[0], int)
        doc_id = row[0]
        doc_ids[demo.key] = doc_id

        conn.execute(
            "UPDATE core.document SET version_group_id = %s WHERE doc_id = %s", (doc_id, doc_id)
        )

        # 块必须原样复制文档的 entity_id（NULL）/doc_type/known_at——
        # check_schema_invariants 的 block_mismatch 检查这三列必须与所属
        # 文档一致；owner_tenant/owner_user 这两列该检查不看，但 RLS 的
        # block_visibility 策略直接读块自己的这两列，不看文档的，必须一起
        # 复制对，否则块的可见性会跟文档对不上。
        conn.execute(
            "INSERT INTO core.doc_block"
            " (doc_id, block_type, section_path, ordinal, is_leaf, content,"
            "  doc_type, publish_at, owner_tenant, owner_user, valid_from, known_at,"
            "  source, source_ref, ingest_run_id)"
            " VALUES (%s, 'paragraph', '', 0, true, %s,"
            "         'user_upload', %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                doc_id,
                f"{SYNTHETIC_MARK} {demo.key} 的正文内容",
                _DEMO_KNOWN_AT,
                demo.owner_tenant,
                demo.owner_user,
                _DEMO_KNOWN_AT.date(),
                _DEMO_KNOWN_AT,
                DEMO_SOURCE,
                f"isolation-demo:{demo.key}",
                _INGEST_RUN_ID,
            ),
        )

    conn.commit()
    return doc_ids


def clear_isolation_demo(conn: psycopg.Connection[tuple[object, ...]], *, dsn: str) -> None:
    """物理删除全部 `source = 'isolation-demo'` 的文档与块。

    **必须用超级用户连接**：`core.document`/`core.doc_block` 的 RLS 只有
    SELECT/INSERT/UPDATE 策略，没有 DELETE 策略（文档只做版本化，
    `006_asof_views_and_roles.sql` 的既定设计）——非超级用户执行 DELETE
    会**静默删除 0 行**且不报任何错误。这里显式断言删除的行数与查到的
    行数一致，删漏了就大声失败，而不是留一堆孤儿数据静默躺在库里。
    """
    _require_loopback(dsn)

    (doc_count,) = conn.execute(
        "SELECT count(*) FROM core.document WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]
    (block_count,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE source = %s", (DEMO_SOURCE,)
    ).fetchone()  # type: ignore[misc]

    block_cur = conn.execute("DELETE FROM core.doc_block WHERE source = %s", (DEMO_SOURCE,))
    if block_cur.rowcount != block_count:
        raise RuntimeError(
            f"预期删除 {block_count} 个块，实际删除 {block_cur.rowcount} 个——"
            "疑似当前连接没有 DELETE 权限（RLS 静默拒绝），请用超级用户连接重试"
        )

    doc_cur = conn.execute("DELETE FROM core.document WHERE source = %s", (DEMO_SOURCE,))
    if doc_cur.rowcount != doc_count:
        raise RuntimeError(
            f"预期删除 {doc_count} 份文档，实际删除 {doc_cur.rowcount} 份——"
            "疑似当前连接没有 DELETE 权限（RLS 静默拒绝），请用超级用户连接重试"
        )

    conn.commit()


__all__ = (
    "DEMO_SOURCE",
    "DEMO_TENANT",
    "DEMO_USER_A",
    "DEMO_USER_B",
    "SYNTHETIC_MARK",
    "AlreadySeeded",
    "NotLoopbackHost",
    "clear_isolation_demo",
    "seed_isolation_demo",
)
