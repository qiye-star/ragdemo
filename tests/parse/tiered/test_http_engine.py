import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ragdemo.adapters.http import RetryPolicy
from ragdemo.parse.tiered.engines import EngineError, EngineUnavailable
from ragdemo.parse.tiered.http_engine import HttpPageEngine, engine_from_url
from ragdemo.parse.tiered.pdfpages import single_page_pdf

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")
HEALTH = {"engine": "paddle", "engine_version": "3.0.0", "device": "cpu"}
NO_RETRY = RetryPolicy(max_attempts=1, backoff_base_s=0)


def _engine(
    health: dict[str, Any], reply: dict[str, Any] | None = None, status: int = 200
) -> tuple[HttpPageEngine, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=health)
        seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={})
        return httpx.Response(200, json=reply)

    e = engine_from_url(
        "paddle", "http://parse.test", transport=httpx.MockTransport(handler), retry=NO_RETRY
    )
    return e, seen


def test_version_comes_from_the_health_endpoint() -> None:
    e, _ = _engine(HEALTH)
    assert (e.name, e.version) == ("paddle", "3.0.0")


def test_a_different_engine_behind_the_port_is_rejected() -> None:
    with pytest.raises(EngineUnavailable, match="健康检查返回异常"):
        _engine({**HEALTH, "engine": "mineru"})


def test_markdown_is_returned_with_images_stripped_and_the_page_is_sent_as_base64() -> None:
    md = "表 91.2\n![](imgs/img_in_table_box_12_34.jpg)"
    reply = {"ok": True, "markdown": md, "elapsed_ms": 5}
    e, seen = _engine(HEALTH, reply)
    out = e.parse_page(single_page_pdf(FIXTURE.read_bytes(), 1), page_no=1)
    assert out.markdown.strip() == "表 91.2" and out.elapsed_ms == 5
    assert seen[0]["page_no"] == 1 and seen[0]["pdf_base64"]


def test_a_content_failure_is_an_engine_error_but_not_unavailable() -> None:
    e, _ = _engine(HEALTH, {"ok": False, "error": "引擎异常：RuntimeError"})
    with pytest.raises(EngineError) as exc:
        e.parse_page(b"%PDF-1.4", page_no=1)
    assert not isinstance(exc.value, EngineUnavailable)


def test_server_errors_become_engine_unavailable() -> None:
    e, _ = _engine(HEALTH, status=503)
    with pytest.raises(EngineUnavailable):
        e.parse_page(b"%PDF-1.4", page_no=1)


def test_markup_is_normalised_on_the_output_path() -> None:
    md = (
        '<div style="x"><html><body><table border="1">'
        "<tr><td>91.2</td></tr></table></body></html></div>"
    )
    e, _ = _engine(HEALTH, {"ok": True, "markdown": md, "elapsed_ms": 1})
    out = e.parse_page(b"%PDF-1.4", page_no=1)
    assert out.markdown == "<table><tr><td>91.2</td></tr></table>"
