"""Schema 不变量与时点泄漏自检。

这些检查比文档更能防止时点语义被悄悄破坏：文档会被忽略，CI 红灯不会。
不变量来自 docs/02-data-model.md §9，泄漏自检来自 docs/03-point-in-time.md §5。
"""

from __future__ import annotations

from datetime import datetime

import psycopg

BITEMPORAL_COLUMNS = frozenset(
    {
        "valid_from",
        "known_at",
        "superseded_at",
        "source",
        "source_ref",
        "ingest_run_id",
        "ingested_at",
    }
)

_LEAK_QUERIES: dict[str, str] = {
    "known_at_before_period_end": """
        SELECT count(*) FROM core.fin_fact WHERE known_at::date < period_end
    """,
    "known_at_equals_ingested_at_on_backfill": """
        SELECT count(*) FROM core.fin_fact
         WHERE known_at = ingested_at AND ingested_at::date - valid_from > 400
    """,
    "superseded_before_known": """
        SELECT (SELECT count(*) FROM core.fin_fact
                 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at)
             + (SELECT count(*) FROM core.doc_block
                 WHERE superseded_at IS NOT NULL AND superseded_at <= known_at)
    """,
    "multiple_live_rows_at_probe": """
        SELECT coalesce(sum(n) - count(*), 0) FROM (
            SELECT count(*) AS n FROM core.fin_fact
             WHERE known_at <= %(probe)s
               AND (superseded_at IS NULL OR superseded_at > %(probe)s)
             GROUP BY entity_id, metric_id, period HAVING count(*) > 1
        ) t
    """,
    "opinion_cites_future_block": """
        SELECT count(*) FROM core.opinion o
          JOIN core.doc_block b ON b.block_id = ANY (o.evidence_blocks)
         WHERE b.known_at > o.as_of
    """,
}


def check_schema_invariants(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    """返回违规描述列表；空列表表示全部通过。"""
    violations: list[str] = []
    registered = [
        str(r[0])
        for r in conn.execute("SELECT table_name::text FROM core.bitemporal_registry").fetchall()
    ]

    for qualified in registered:
        schema, _, table = qualified.partition(".")

        cols = {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            ).fetchall()
        }
        missing = BITEMPORAL_COLUMNS - cols
        if missing:
            violations.append(f"{qualified} 缺少时点公共字段: {sorted(missing)}")

        has_check = conn.execute(
            "SELECT count(*) > 0 FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'c' "
            "  AND pg_get_constraintdef(oid) ILIKE %s",
            (qualified, "%superseded_at%known_at%"),
        ).fetchone()
        if not (has_check and has_check[0]):
            violations.append(f"{qualified} 缺少 superseded_at > known_at 的 CHECK 约束")

        has_view = conn.execute(
            "SELECT count(*) > 0 FROM information_schema.views "
            "WHERE table_schema = 'asof' AND table_name = %s",
            (table,),
        ).fetchone()
        if not (has_view and has_view[0]):
            violations.append(f"{qualified} 没有对应的 asof.{table} 视图")

        # 不变量 5（收窄）：读角色对**登记在册的时点表**不得有 SELECT。
        # 文档原文是「对 core schema 无 SELECT」，与 docs/03 §4.4 要求给
        # entity / node_metric / propagation_rule 授予 SELECT 直接冲突；
        # 按 docs/10-roadmap.md P0 验收的字面要求（core.fin_fact 被拒）收窄。
        readable = conn.execute(
            "SELECT has_table_privilege('app_read', %s, 'SELECT')", (qualified,)
        ).fetchone()
        if readable and readable[0]:
            violations.append(f"app_read 不应对时点表 {qualified} 有 SELECT 权限")

    block_mismatch = conn.execute(
        "SELECT count(*) FROM ("
        "  SELECT b.block_id FROM core.doc_block b JOIN core.document d USING (doc_id)"
        "   WHERE b.entity_id IS DISTINCT FROM d.entity_id"
        "      OR b.doc_type  IS DISTINCT FROM d.doc_type"
        "      OR b.known_at  IS DISTINCT FROM d.known_at"
        "   LIMIT 1000) t"
    ).fetchone()
    if block_mismatch and block_mismatch[0]:
        violations.append(f"doc_block 的反规范化列与 document 不一致: {block_mismatch[0]} 行")

    # 不变量 6（新增）：core / asof 的对象不得由超级用户持有。
    # 超级用户无条件绕过 RLS（FORCE 也拦不住），而 asof.* 是普通视图、
    # 按属主身份执行——属主一退回超级用户，行级隔离就静默失效，
    # 且 tests/db/test_asof_layer.py 的隔离测试仍会「通过」。
    superuser_owned = [
        str(r[0])
        for r in conn.execute(
            "SELECT n.nspname || '.' || c.relname "
            "  FROM pg_class c "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            "  JOIN pg_roles rl ON rl.oid = c.relowner "
            " WHERE n.nspname IN ('core','asof') AND c.relkind IN ('r','v') AND rl.rolsuper "
            " ORDER BY 1"
        ).fetchall()
    ]
    if superuser_owned:
        violations.append(f"以下 core/asof 对象仍由超级用户持有，RLS 会被绕过: {superuser_owned}")

    return violations


def check_point_in_time_leaks(
    conn: psycopg.Connection[tuple[object, ...]], probe_as_of: datetime
) -> dict[str, int]:
    """跑 docs/03-point-in-time.md §5 的自检查询，返回有违规的项与行数。

    返回空字典表示没有泄漏。
    """
    found: dict[str, int] = {}
    for name, sql in _LEAK_QUERIES.items():
        row = conn.execute(sql, {"probe": probe_as_of}).fetchone()
        if row is None:
            raise RuntimeError(f"泄漏自检 {name} 没有返回任何行，这不应该发生")
        if row[0]:
            found[name] = int(row[0])
    return found
