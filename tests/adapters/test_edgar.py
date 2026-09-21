"""EDGAR 适配器：known_at 用 acceptanceDateTime（精确到秒），不用 filingDate。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ragdemo.adapters.base import Adapter, RawResponse
from ragdemo.adapters.edgar import TRACKED_FORMS, EdgarAdapter
from tests.contracts.adapter_contract import AdapterContract

FIXTURES = Path("tests/fixtures/edgar")


def _raw() -> RawResponse:
    return RawResponse(
        provider="edgar", endpoint="/submissions/CIK0001045810.json", params={},
        payload=json.loads(
            (FIXTURES / "submissions_0001045810.json").read_text(encoding="utf-8")
        ),
        http_status=200, fetched_at=datetime.now(UTC),
    )


class TestEdgarContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return EdgarAdapter.for_replay(FIXTURES)


def test_only_tracked_forms_are_returned() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    assert {f.form_type for f in filings} == {"10-Q", "8-K"}
    assert "DEF 14A" not in TRACKED_FORMS


def test_known_at_is_acceptance_datetime_to_the_second() -> None:
    """EDGAR 给出精确到秒的受理时间，直接用，不要退化成日期。"""
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.acceptance_datetime == datetime(2024, 11, 20, 16, 31, 24, tzinfo=UTC)


def test_document_url_is_constructed_correctly() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.document_url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581024000316/nvda-20241027.htm"
    )


def test_two_filings_same_day_are_ordered_by_acceptance_time() -> None:
    """同日两份申报，靠 acceptanceDateTime 才能分出先后。"""
    filings = [f for f in EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw())]
    same_day = sorted(
        (f for f in filings if f.filing_date.isoformat() == "2024-11-20"),
        key=lambda f: f.acceptance_datetime,
    )
    assert [f.form_type for f in same_day] == ["8-K", "10-Q"]
