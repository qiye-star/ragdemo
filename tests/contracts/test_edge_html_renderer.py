"""Edge 无头渲染的契约测试：需要本机装了 Edge，默认跳过，不进 CI。

本机手动跑：设 RAGDEMO_RUN_LOCAL_EDGE=1 后 `pytest tests/contracts/test_edge_html_renderer.py`。
"""

from __future__ import annotations

import os

import pytest

from ragdemo.adapters.html_render import EdgeHeadlessRenderer
from tests.contracts.html_renderer_contract import HtmlRendererContract

pytestmark = [
    pytest.mark.local_edge,
    pytest.mark.skipif(
        os.environ.get("RAGDEMO_RUN_LOCAL_EDGE") != "1",
        reason="需要本机 Edge；设 RAGDEMO_RUN_LOCAL_EDGE=1 手动跑",
    ),
]


class TestEdgeHeadlessRenderer(HtmlRendererContract):
    def make_renderer(self) -> EdgeHeadlessRenderer:
        return EdgeHeadlessRenderer()
