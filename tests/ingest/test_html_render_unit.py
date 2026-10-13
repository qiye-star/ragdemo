"""EdgeHeadlessRenderer 的纯逻辑部分（不启动浏览器）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragdemo.adapters.html_render import (
    EdgeHeadlessRenderer,
    HtmlRenderError,
    renderer_label_from_pdf_creator,
)


def test_missing_edge_binary_fails_loudly(tmp_path: Path) -> None:
    r = EdgeHeadlessRenderer(edge_path=str(tmp_path / "nope.exe"))
    with pytest.raises(HtmlRenderError, match="Edge"):
        r.render(b"<html></html>")


def test_label_extracts_edge_version() -> None:
    creator = "Mozilla/5.0 ... HeadlessChrome/155.0.0.0 Safari/537.36 Edg/155.0.3800.1"
    assert renderer_label_from_pdf_creator(creator) == "msedge-headless:155.0.3800.1"


def test_label_falls_back_to_unknown() -> None:
    assert renderer_label_from_pdf_creator(None) == "msedge-headless:unknown"
