import base64
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("shim", Path("infra/parse-engines/common/shim.py"))
assert _spec and _spec.loader
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)


@pytest.mark.parametrize("value", [None, "", "abc", "-5", "0", "1.5"])
def test_missing_or_non_positive_length_is_a_400(value: str | None) -> None:
    with pytest.raises(shim.RequestError) as exc:
        shim.parse_length(value)
    assert exc.value.status == 400


def test_oversized_body_is_rejected_before_reading() -> None:
    with pytest.raises(shim.RequestError) as exc:
        shim.parse_length(str(shim.MAX_BODY + 1))
    assert exc.value.status == 413
    assert shim.parse_length(str(shim.MAX_BODY)) == shim.MAX_BODY


def _body(raw: bytes) -> bytes:
    return json.dumps({"pdf_base64": base64.b64encode(raw).decode()}).encode()


def test_decode_requires_a_pdf() -> None:
    assert shim.decode_pdf(_body(b"%PDF-1.4 x")) == b"%PDF-1.4 x"
    for bad in (b"{", _body(b"hello"), json.dumps({"pdf_base64": "!!"}).encode()):
        with pytest.raises(shim.RequestError) as exc:
            shim.decode_pdf(bad)
        assert exc.value.status == 400


def test_exactly_one_page_is_required() -> None:
    shim.require_single_page(b"%PDF", lambda _raw: 1)
    for n in (0, 2):
        with pytest.raises(shim.RequestError) as exc:
            shim.require_single_page(b"%PDF", lambda _raw, n=n: n)
        assert exc.value.status == 400


def test_an_unreadable_pdf_is_a_400() -> None:
    def boom(_raw: bytes) -> int:
        raise ValueError("broken")

    with pytest.raises(shim.RequestError) as exc:
        shim.require_single_page(b"%PDF", boom)
    assert exc.value.status == 400


def test_count_pages_uses_pdfium_and_closes_the_document() -> None:
    pytest.importorskip("pypdfium2")
    from ragdemo.parse.tiered.pdfpages import single_page_pdf, sub_pdf

    src = Path("tests/fixtures/pdf/table_sample.pdf").read_bytes()
    assert shim.count_pages(single_page_pdf(src, 1)) == 1
    two = sub_pdf(src, [1, 1])
    assert shim.count_pages(two) == 2


def _serve_on_free_port() -> int:
    import socket
    import threading

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    threading.Thread(
        target=shim.serve,
        args=("fake", "1", "cpu", lambda _p: "# md"),
        kwargs={"port": port},
        daemon=True,
    ).start()
    import time

    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return port
        except OSError:
            time.sleep(0.1)
    raise AssertionError("shim did not start")


def test_handler_wires_request_errors_to_http_statuses() -> None:
    import http.client

    port = _serve_on_free_port()

    def post(headers: dict[str, str], body: bytes = b"") -> tuple[int, dict[str, object]]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/v1/parse-page", skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        return r.status, json.loads(r.read())

    assert post({})[0] == 400  # 缺 Content-Length
    assert post({"Content-Length": "-3"})[0] == 400
    assert post({"Content-Length": str(shim.MAX_BODY + 1)})[0] == 413
    body = json.dumps({"pdf_base64": base64.b64encode(b"%PDF-1.4 junk").decode()}).encode()
    status, payload = post({"Content-Length": str(len(body))}, body)
    assert status == 400 and payload["ok"] is False  # 不是可读的 1 页 PDF
