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
