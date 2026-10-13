"""T6 接线：绑定上下文失败不能拖垮文档解析（上下文是附加品）。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ragdemo.parse import textin
from ragdemo.parse.textin import blocks_from_detail


def _cell(r: int, c: int, text: object) -> dict[str, Any]:
    return {
        "row": r,
        "col": c,
        "row_span": 1,
        "col_span": 1,
        "text": text,
        "position": [0, 0, 10, 0, 10, 10, 0, 10],
    }


def _table(pid: object, **kw: object) -> dict[str, Any]:
    return {
        "type": "table",
        "page_id": 1,
        "paragraph_id": pid,
        "content": 0,
        "position": [0, 0, 10, 0, 10, 10, 0, 10],
        "cells": [
            _cell(0, 0, "项目"),
            _cell(0, 1, "本期"),
            _cell(1, 0, "营业收入"),
            _cell(1, 1, "1,000"),
        ],
        **kw,
    }


def _blocks(detail: list[dict[str, Any]]) -> list[Any]:
    return blocks_from_detail(detail, page_dims={1: (100, 100)}, engine="e", json_ref="k")


def _content(blocks: list[Any]) -> list[tuple[Any, ...]]:
    return [(b.block_type, b.content, b.table_html, b.page, b.bbox, b.section_path) for b in blocks]


def test_binding_exception_gives_none_context_and_identical_blocks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    detail = [
        {"type": "paragraph", "page_id": 1, "paragraph_id": 1, "text": "单位：万元SECRET"},
        _table(2),
    ]
    good = _blocks(detail)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("单位：万元SECRET")

    monkeypatch.setattr(textin, "bind_context", boom)
    with caplog.at_level(logging.WARNING):
        bad = _blocks(detail)
    assert _content(bad) == _content(good)
    table = bad[1].table
    assert table is not None and table.context is None and table.grid is not None
    rec = next(r for r in caplog.records if "context skipped" in r.getMessage())
    assert rec.json_ref == "k" and rec.table_index == 0 and rec.page_id == 1  # type: ignore[attr-defined]
    assert "SECRET" not in caplog.text


def test_malformed_items_never_break_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    huge = "单位：" + "万" * 100_000
    detail: list[dict[str, Any]] = [
        {"type": "paragraph", "page_id": None, "paragraph_id": None, "text": None},
        {"type": "paragraph", "text": huge},
        {"type": "paragraph", "page_id": 1, "paragraph_id": 2, "text": "\x00퟿币种：￥$$"},
        {"type": "paragraph"},
        _table(3, caption_id="not-a-mapping"),
        _table(4, caption_id={"page_id": 1}),
        _table(None, caption_id={"page_id": None, "paragraph_id": None}, page_id=None),
        _table(5, caption_id={"page_id": 1, "paragraph_id": 2}, content=None),
        {**_table(6), "cells": [_cell(0, 0, None), _cell(0, 1, "单位：" + "元" * 5000)]},
    ]
    with_ctx = _blocks(detail)
    monkeypatch.setattr(textin, "bind_context", lambda *a, **k: None)
    without = _blocks(detail)
    assert _content(with_ctx) == _content(without)
    assert any(b.table is not None for b in with_ctx)
