"""真实（数据库）数据接口实现通过与 Mock 完全相同的契约测试（阶段 2.10）。

把 `sample.SAMPLE` 种进临时库、用 `PgDataApi` 跑同一套断言：时点过滤、可见性、溯源、
错误语义在两种实现上行为一致。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ragdemo.dataapi.pg import PgDataApi
from ragdemo.dataapi.sample_seed import seed_sample
from ragdemo.embed.mock import MockEmbedder
from ragdemo.retrieval.rerank import MockReranker
from ragdemo.retrieval.service import RetrievalService
from ragdemo_core.db.migrate import migrate
from tests.contracts.dataapi_contract import DataApiContract

MIGRATIONS = Path("db/migrations")


@pytest.mark.db
class TestPgDataApi(DataApiContract):
    @pytest.fixture(autouse=True)
    def _db(self, temp_db: str) -> Iterator[None]:
        conn = psycopg.connect(temp_db)
        migrate(conn, MIGRATIONS)
        seed_sample(conn)
        self._conn = conn
        yield
        conn.close()

    def make_api(self) -> PgDataApi:
        retrieval = RetrievalService(self._conn, MockEmbedder(), MockReranker())
        return PgDataApi(self._conn, retrieval=retrieval)
