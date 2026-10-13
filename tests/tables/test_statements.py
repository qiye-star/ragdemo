from __future__ import annotations

from ragdemo.tables.grid import GridCell, TableGrid
from ragdemo.tables.headers import infer_headers
from ragdemo.tables.statements import statement_kind


def _grid(*labels: str) -> TableGrid:
    rows = [("项目", "本期")] + [(lab, "1,000") for lab in labels]
    cells = tuple(GridCell(r, c, 1, 1, t) for r, row in enumerate(rows) for c, t in enumerate(row))
    return infer_headers(TableGrid(cells=cells, source_engine="x"))


def test_balance_sheet() -> None:
    assert statement_kind(_grid("流动资产合计", "资产总计")) == "balance_sheet"


def test_income() -> None:
    assert statement_kind(_grid("营业收入", "营业成本", "净利润")) == "income"


def test_cash_flow() -> None:
    assert (
        statement_kind(
            _grid(
                "经营活动产生的现金流量净额",
                "投资活动产生的现金流量净额",
                "筹资活动产生的现金流量净额",
            )
        )
        == "cash_flow"
    )


def test_key_figures_single_known_label() -> None:
    assert statement_kind(_grid("研发费用")) == "key_figures"


def test_other_shareholders() -> None:
    assert statement_kind(_grid("张三", "李四")) == "other"


def test_label_whitespace_and_br_normalised() -> None:
    assert statement_kind(_grid("流动资产<br>合计", "资 产 总 计")) == "balance_sheet"


def test_order_balance_before_income() -> None:
    assert (
        statement_kind(_grid("流动资产合计", "资产总计", "营业收入", "营业成本")) == "balance_sheet"
    )
