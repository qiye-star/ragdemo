from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from ragdemo.adapters.http import RetryPolicy
from ragdemo.parse.tiered.http_engine import HttpPageEngine, engine_from_url
from tests.contracts.page_engine_contract import PageEngineContract

RECORDED = Path("tests/fixtures/parse_engines/paddle_table_sample.json")


def replay_engine(name: str, recorded: Path) -> HttpPageEngine:
    rec = json.loads(recorded.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=rec["health"])
        raw = base64.b64decode(json.loads(request.content)["pdf_base64"])
        if not raw.startswith(b"%PDF"):
            return httpx.Response(200, json={"ok": False, "error": "不是有效的 PDF"})
        return httpx.Response(200, json=rec["parse"])

    return engine_from_url(
        name,  # type: ignore[arg-type]
        "http://parse.test",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=1, backoff_base_s=0),
    )


class TestRecordedPaddleEngine(PageEngineContract):
    def make_engine(self) -> HttpPageEngine:
        return replay_engine("paddle", RECORDED)


@pytest.mark.live_engine
class TestLivePaddleEngine(PageEngineContract):
    def make_engine(self) -> HttpPageEngine:
        url = os.environ.get("PARSE_PADDLE_BASE_URL")
        if not url:
            pytest.skip("PARSE_PADDLE_BASE_URL 未设置")
        return engine_from_url("paddle", url)
