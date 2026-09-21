"""MockAnnouncementProvider 通过全部公告契约，并正确应用披露延迟。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ragdemo.adapters.announcements import AnnouncementProvider, known_at_for
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from tests.contracts.announcement_contract import AnnouncementContract


class TestMockAnnouncementContract(AnnouncementContract):
    def make_provider(self) -> AnnouncementProvider:
        return MockAnnouncementProvider()


def test_known_at_adds_disclosure_lag() -> None:
    """T+1 批量供应商：我们能获知的时刻比公告发布时刻晚一天。"""
    publish_at = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert known_at_for(publish_at, timedelta(days=1)) == publish_at + timedelta(days=1)


def test_zero_lag_means_known_at_equals_publish_at() -> None:
    publish_at = datetime(2024, 10, 28, 18, 32, tzinfo=UTC)
    assert known_at_for(publish_at, timedelta(0)) == publish_at


def test_mock_exposes_a_table_block_with_section_path() -> None:
    """Mock 数据必须包含表格块，否则下游切块器的表格分支永远测不到。"""
    provider = MockAnnouncementProvider()
    docs = [
        provider.normalize(raw)
        for raw in provider.list_documents(
            _ctx(), since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]
    tables = [b for d in docs for b in d.blocks if b.block_type == "table"]
    assert tables
    assert all(t.section_path for t in tables)


def test_correction_document_points_at_the_original() -> None:
    provider = MockAnnouncementProvider()
    docs = [
        provider.normalize(raw)
        for raw in provider.list_documents(
            _ctx(), since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]
    corrections = [d for d in docs if d.is_correction]
    assert corrections
    assert all(c.supersedes_provider_doc_id for c in corrections)


def _ctx() -> FetchContext:
    return FetchContext(ingest_run_id="t", partition_date=date(2024, 10, 28))
