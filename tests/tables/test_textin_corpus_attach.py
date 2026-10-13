"""真实缓存语料上，经 blocks_from_detail 的挂载路径校验网格（语料不在时跳过）。

golden 是在改动前基线（89b64ba）上生成的每文件块字段哈希，只含公开公告的元数据哈希。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ragdemo.parse.textin import _page_offset, blocks_from_detail, page_dims_from_metrics
from ragdemo.tables.render import grid_html, grid_markdown

_ROOT = Path(__file__).resolve().parents[2] / "data" / "blob" / "parse" / "textin"
_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "tables_textin_golden.json"


@pytest.mark.skipif(not _ROOT.exists(), reason="no cached TextIn corpus")
def test_corpus_attach_path_is_faithful_and_block_output_unchanged() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    seen = 0
    for f in sorted(_ROOT.rglob("*.json")):
        rel = f.relative_to(_ROOT).as_posix()
        payload = json.loads(f.read_text(encoding="utf-8"))
        detail = (payload.get("result") or {}).get("detail") or []
        offset = _page_offset(detail)
        blocks = blocks_from_detail(
            detail,
            page_dims=page_dims_from_metrics(payload.get("metrics"), offset),
            engine="e",
            json_ref=rel,
        )
        rows = [
            [b.block_type, b.content, b.table_html, b.page, b.bbox, b.section_path, b.level]
            for b in blocks
        ]
        digest = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
        assert digest == golden[rel], rel

        raw_tables = [
            i
            for i in detail
            if i.get("type") == "table"
            and int(i.get("content", 0) or 0) != 1
            and (i.get("cells") or [])
        ]
        tables = [b for b in blocks if b.block_type == "table"]
        assert len(tables) == len(raw_tables), rel
        for b, raw in zip(tables, raw_tables, strict=True):
            assert b.table is not None, rel
            g = b.table.grid
            assert grid_markdown(g) == b.content
            assert grid_html(g) == b.table_html
            for cell, rc in zip(g.cells, raw["cells"], strict=True):
                pid = rc.get("page_id", raw.get("page_id"))
                assert cell.page == int(pid) + offset
                if cell.bbox is not None:
                    assert all(0.0 <= v <= 1.0 for v in cell.bbox)
            seen += 1
    assert seen > 0
