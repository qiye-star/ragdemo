"""任何 AnnouncementProvider 实现都必须通过的 6 条契约。

选定供应商后，新写的 adapter 必须原样通过这些测试——这是「定接口不定供应商」
能兑现的前提（adr/0005）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ragdemo.adapters.announcements import AnnouncementProvider
from ragdemo.adapters.base import FetchContext


class AnnouncementContract:
    def make_provider(self) -> AnnouncementProvider:
        raise NotImplementedError

    def make_context(self) -> FetchContext:
        return FetchContext(ingest_run_id="contract", partition_date=date(2024, 10, 28))

    def _documents(self) -> list[object]:
        provider = self.make_provider()
        ctx = self.make_context()
        return [
            provider.normalize(raw)
            for raw in provider.list_documents(
                ctx, since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
            )
        ]

    def test_publish_at_is_timezone_aware(self) -> None:
        for doc in self._documents():
            assert doc.publish_at.tzinfo is not None  # type: ignore[attr-defined]

    def test_block_ordinals_start_at_zero_and_are_contiguous(self) -> None:
        for doc in self._documents():
            ordinals = [b.ordinal for b in doc.blocks]  # type: ignore[attr-defined]
            assert ordinals == list(range(len(ordinals)))

    def test_every_block_has_nonempty_content(self) -> None:
        for doc in self._documents():
            for block in doc.blocks:  # type: ignore[attr-defined]
                assert block.content.strip()

    def test_block_types_are_from_the_allowed_set(self) -> None:
        allowed = {"paragraph", "table", "figure", "title"}
        for doc in self._documents():
            for block in doc.blocks:  # type: ignore[attr-defined]
                assert block.block_type in allowed

    def test_content_hash_is_stable_across_calls(self) -> None:
        first = {d.provider_doc_id: d.content_hash for d in self._documents()}  # type: ignore[attr-defined]
        second = {d.provider_doc_id: d.content_hash for d in self._documents()}  # type: ignore[attr-defined]
        assert first == second

    def test_disclosure_lag_is_a_non_negative_timedelta(self) -> None:
        lag = self.make_provider().disclosure_lag
        assert isinstance(lag, timedelta)
        assert lag >= timedelta(0)
