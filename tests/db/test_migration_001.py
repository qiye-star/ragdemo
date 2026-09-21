"""迁移 001：扩展、schema、枚举。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
EXPECTED_SCHEMAS = {"core", "asof", "evals", "audit", "orchestration"}
EXPECTED_ENUMS = {
    "entity_type",
    "entity_status",
    "relation_type",
    "metric_role",
    "block_type",
    "opinion_direction",
    "score_horizon",
    "score_track",
    "scorer",
    "confidence",
}


@pytest.mark.db
def test_schemas_and_enums_created(temp_db: str) -> None:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)

        schemas = {
            r[0]
            for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                (sorted(EXPECTED_SCHEMAS),),
            ).fetchall()
        }
        enums = {
            r[0]
            for r in conn.execute(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'core' AND t.typtype = 'e'"
            ).fetchall()
        }
        exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}

    assert schemas == EXPECTED_SCHEMAS
    assert enums == EXPECTED_ENUMS
    assert {"vector", "pg_search", "pgcrypto", "pg_trgm"} <= exts
