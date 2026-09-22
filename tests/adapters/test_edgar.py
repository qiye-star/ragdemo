"""EDGAR 适配器：known_at 用 acceptanceDateTime（精确到秒），不用 filingDate。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ragdemo.adapters.base import Adapter, FetchContext, RawResponse
from ragdemo.adapters.edgar import TRACKED_FORMS, EdgarAdapter
from ragdemo.adapters.http import HttpClient, RetryPolicy, TokenBucket
from tests.contracts.adapter_contract import AdapterContract

FIXTURES = Path("tests/fixtures/edgar")


def _raw() -> RawResponse:
    return RawResponse(
        provider="edgar",
        endpoint="/submissions/CIK0001045810.json",
        params={},
        payload=json.loads((FIXTURES / "submissions_0001045810.json").read_text(encoding="utf-8")),
        http_status=200,
        fetched_at=datetime.now(UTC),
    )


class TestEdgarContract(AdapterContract):
    def make_adapter(self) -> Adapter:
        return EdgarAdapter.for_replay(FIXTURES)


def test_only_tracked_forms_are_returned() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    assert {f.form_type for f in filings} == {"10-Q", "8-K"}
    assert "DEF 14A" not in TRACKED_FORMS


def test_known_at_is_acceptance_datetime_to_the_second() -> None:
    """EDGAR 给出精确到秒的受理时间，直接用，不要退化成日期。

    acceptanceDateTime 的 'Z' 后缀具有误导性：字段实际报的是美国东部
    时间，不是 UTC。夹具值 '2024-11-20T16:31:24.000Z' 应按东部时间
    解析——11 月 20 日已过夏令时切换（2024 年 11 月 3 日转回 EST，
    UTC-5），所以 16:31:24 ET == 21:31:24 UTC。
    """
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.acceptance_datetime == datetime(2024, 11, 20, 21, 31, 24, tzinfo=UTC)


def test_document_url_is_constructed_correctly() -> None:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    tenq = next(f for f in filings if f.form_type == "10-Q")
    assert tenq.document_url == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000316/nvda-20241027.htm"
    )


def test_two_filings_same_day_are_ordered_by_acceptance_time() -> None:
    """同日两份申报，靠 acceptanceDateTime 才能分出先后。"""
    filings = [f for f in EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw())]
    same_day = sorted(
        (f for f in filings if f.filing_date.isoformat() == "2024-11-20"),
        key=lambda f: f.acceptance_datetime,
    )
    assert [f.form_type for f in same_day] == ["8-K", "10-Q"]


def test_http_client_payload_flattens_submissions_envelope() -> None:
    """真实 HttpClient 分支也要展开 submissions envelope，
    防止 known_at() 因 acceptanceDateTime 嵌套而 KeyError。"""
    # 模拟未展开的 EDGAR 原始响应
    envelope = json.loads((FIXTURES / "submissions_0001045810.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    transport = httpx.MockTransport(handler)
    client = HttpClient(
        provider="edgar",
        base_url="https://data.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=transport,
    )
    adapter = EdgarAdapter(client=client)
    ctx = FetchContext(ingest_run_id="test-run", partition_date=datetime.now(UTC).date())

    # 获取 fetch() 的输出
    responses = list(adapter.fetch(ctx, cik=1045810))
    assert len(responses) == 1
    raw = responses[0]

    # 验证 payload 已展开为行列表（每行有直接顶层的 acceptanceDateTime）
    assert isinstance(raw.payload, list)
    assert len(raw.payload) == 3
    for row in raw.payload:
        assert isinstance(row, dict)
        assert "acceptanceDateTime" in row
        # 验证 known_at() 能正确处理该记录（不会因嵌套结构 KeyError）
        known_at = adapter.known_at(row)
        assert known_at.tzinfo is not None


# --- fetch_full_text（阶段 F：F5，全文抓取此前完全没有代码）------------------


def _filing_ref() -> object:
    filings = list(EdgarAdapter.for_replay(FIXTURES).parse_filings(_raw()))
    return next(f for f in filings if f.form_type == "10-Q")


def test_fetch_full_text_requests_the_documents_own_path_with_a_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<html>10-Q body</html>")

    client = HttpClient(
        provider="edgar-archives",
        base_url="https://www.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=httpx.MockTransport(handler),
        default_headers={"User-Agent": "ragdemo research contact@example.invalid"},
    )
    ref = _filing_ref()

    body = EdgarAdapter.for_replay(FIXTURES).fetch_full_text(ref, client)  # type: ignore[arg-type]

    assert body == b"<html>10-Q body</html>"
    assert len(seen) == 1
    assert seen[0].url.path == (
        "/Archives/edgar/data/1045810/000104581024000316/nvda-20241027.htm"
    )
    assert seen[0].headers["user-agent"] == "ragdemo research contact@example.invalid"


def test_fetch_full_text_raises_on_persistent_failure() -> None:
    client = HttpClient(
        provider="edgar-archives",
        base_url="https://www.sec.gov",
        policy=RetryPolicy(max_attempts=1, backoff_base_s=0.0),
        bucket=TokenBucket(rate_per_minute=10_000),
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        default_headers={"User-Agent": "ragdemo research contact@example.invalid"},
    )
    ref = _filing_ref()

    try:
        EdgarAdapter.for_replay(FIXTURES).fetch_full_text(ref, client)  # type: ignore[arg-type]
    except Exception as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("expected an UpstreamUnavailable for a 404")
