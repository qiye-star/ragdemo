from __future__ import annotations

import json
from pathlib import Path

from ragdemo.parse.tiered.types import PageRoute, route_to_dict
from ragdemo.tables.page_engines import routes_from_sidecar, table_engine
from ragdemo_core.blob import LocalBlobStore

ROUTE = PageRoute(page=4, tier="L1", engine="paddle:3.0", text_layer=True, reasons=("l0_failed",))


def test_routes_from_sidecar(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    blob.put("p/x.json", json.dumps({"routes": [route_to_dict(ROUTE)]}).encode())
    routes = routes_from_sidecar(blob, "p/x.json")
    assert routes[4].tier == "L1" and routes[4].engine == "paddle:3.0"


def test_missing_or_malformed_sidecar_is_empty(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    assert routes_from_sidecar(blob, "nope.json") == {}
    blob.put("a.json", b"{}")
    blob.put("b.json", b"not json")
    blob.put("c.json", b'{"routes": [{"page": "x"}]}')
    blob.put("d.json", b'{"routes": "oops"}')
    blob.put("e.json", b"[1]")
    for key in "abcde":
        assert routes_from_sidecar(blob, f"{key}.json") == {}


def test_duplicate_page_routes_drop_that_page(tmp_path: Path) -> None:
    blob = LocalBlobStore(tmp_path)
    other = PageRoute(page=4, tier="L2", engine="a+b", text_layer=True)
    r5 = PageRoute(page=5, tier="L1", engine="paddle:3.0", text_layer=True)
    doc = {"routes": [route_to_dict(ROUTE), route_to_dict(other), route_to_dict(r5)]}
    blob.put("d.json", json.dumps(doc).encode())
    assert set(routes_from_sidecar(blob, "d.json")) == {5}


def test_table_engine() -> None:
    assert table_engine(None, "textin:v+fp") == ("textin:v+fp", None)
    assert table_engine(ROUTE, "tiered:v1+abc") == ("L1:paddle:3.0", "L1")
