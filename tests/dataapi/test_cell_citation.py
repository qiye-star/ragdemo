"""CellCitation 渲染：前缀与 BlockCitation 一致，行列 0 基。"""

from __future__ import annotations

import pytest

from ragdemo.dataapi.contract import BlockCitation, CellCitation


def test_render_extends_block_citation_prefix() -> None:
    assert CellCitation(1885, 9, 12, 1).render() == "[1885 | 9 | r12c1]"
    assert CellCitation(1885, 9, 12, 1).render().startswith(BlockCitation(1885, 9).render()[:-1])
    assert CellCitation(7, None, 0, 0).render() == "[7 | — | r0c0]"


def test_cell_citation_is_frozen() -> None:
    c = CellCitation(1, 2, 3, 4)
    with pytest.raises(AttributeError):
        c.row = 9  # type: ignore[misc]
