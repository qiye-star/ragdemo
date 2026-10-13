from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from ragdemo.parse.local import pipeline
from ragdemo.parse.local.number_closure import numbers_in
from ragdemo.parse.local.pipeline import parse_pages_deterministic
from ragdemo.parse.local.typeset import TypesetResult

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")
BODY = [
    "Revenue grew strongly during the period and management expects momentum",
    "to continue into the next quarter given the order backlog of 42 units",
    "and the stable gross margin of 31.5 percent reported by the company.",
]


def _pdf(pages: Sequence[Sequence[tuple[str, float, float, str, float]]]) -> bytes:
    """pages: 每页若干 (文字, x, y_from_top, 字体, 字号)。"""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    for items in pages:
        for text, x, y, font, size in items:
            c.setFont(font, size)
            c.drawString(x, 800 - y, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _body_page(title: str = "Annual Overview") -> list[tuple[str, float, float, str, float]]:
    rows = [("Helvetica-Bold", 20.0)]
    items = [(title, 60.0, 80.0, rows[0][0], rows[0][1])]
    items += [(t, 60.0, 130.0 + i * 15, "Helvetica", 10.0) for i, t in enumerate(BODY * 4)]
    return items


def test_a_text_page_is_ok_with_a_heading_and_the_table_from_coordinates() -> None:
    (page,) = parse_pages_deterministic(FIXTURE.read_bytes())
    assert page.status == "ok" and page.violations == [] and page.reason == ""
    assert "| Revenue | 91.2 | 60.1 |" in page.markdown
    assert numbers_in(page.markdown) == numbers_in(page.source_text)


def test_heading_gets_hash_and_body_text_is_unchanged() -> None:
    (page,) = parse_pages_deterministic(_pdf([_body_page()]))
    assert page.status == "ok"
    assert page.markdown.splitlines()[0] == "# Annual Overview"
    assert "order backlog of 42 units" in page.markdown


def test_a_page_without_a_text_layer_is_handed_back(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as fh:
        w.write(fh)
    (page,) = parse_pages_deterministic(path.read_bytes())
    assert page.status == "no_text_layer" and page.markdown == "" and page.reason


def test_a_two_column_page_is_layout_unsupported() -> None:
    rows = []
    for i in range(14):
        rows.append(
            (f"left column sentence number {i} goes here", 60.0, 100.0 + i * 15, "Helvetica", 10.0)
        )
        rows.append(
            (
                f"right column sentence number {i} goes there",
                330.0,
                100.0 + i * 15,
                "Helvetica",
                10.0,
            )
        )
    (page,) = parse_pages_deterministic(_pdf([rows]))
    assert page.status == "layout_unsupported" and page.markdown == ""
    assert "多栏" in page.reason and page.source_text


def test_stripped_header_and_page_number_are_recorded_not_output() -> None:
    pages = []
    for i in range(1, 6):
        items = _body_page()
        items.append(("ACME Holdings 688041 Report", 60.0, 30.0, "Helvetica", 9.0))
        items.append((f"{i} / 5", 280.0, 780.0, "Helvetica", 9.0))
        pages.append(items)
    results = parse_pages_deterministic(_pdf(pages))
    assert [r.status for r in results] == ["ok"] * 5
    assert "688041" not in results[2].markdown and "688041" in results[2].stripped_text


def _patched(monkeypatch: pytest.MonkeyPatch, markdown: str) -> None:
    def fake(pages: Sequence[object]) -> list[TypesetResult]:
        return [TypesetResult("ok", markdown, "", []) for _ in pages]

    monkeypatch.setattr(pipeline, "typeset_document", fake)


def test_a_typesetter_that_changes_a_digit_is_caught_as_closure_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = parse_pages_deterministic(FIXTURE.read_bytes())[0]
    _patched(monkeypatch, good.markdown.replace("91.2", "91.3"))
    (page,) = parse_pages_deterministic(FIXTURE.read_bytes())
    assert page.status == "closure_failed" and page.markdown == ""
    assert "91.3" in page.violations and "91.2" in page.reason  # 新增 91.3，同时丢了 91.2


def test_a_typesetter_that_changes_a_unit_character_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = _pdf([_body_page()])
    good = parse_pages_deterministic(pdf)[0]
    _patched(monkeypatch, good.markdown.replace("units", "unitz"))
    (page,) = parse_pages_deterministic(pdf)
    assert page.status == "closure_failed" and "字符" in page.reason


def test_a_typesetter_that_drops_text_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = _pdf([_body_page()])
    good = parse_pages_deterministic(pdf)[0]
    _patched(monkeypatch, good.markdown.replace("stable gross margin ", ""))
    assert parse_pages_deterministic(pdf)[0].status == "closure_failed"


def test_a_typesetter_that_loses_a_table_verbatim_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = parse_pages_deterministic(FIXTURE.read_bytes())[0]

    def fake(pages: Sequence[object]) -> list[TypesetResult]:
        table = "| Revenue | 60.1 | 91.2 |"  # 数字都在，但列序变了
        return [
            TypesetResult(
                "ok",
                good.markdown.replace("| Revenue | 91.2 | 60.1 |", table),
                "",
                ["| Revenue | 91.2 | 60.1 |"],
            )
        ]

    monkeypatch.setattr(pipeline, "typeset_document", fake)
    (page,) = parse_pages_deterministic(FIXTURE.read_bytes())
    assert page.status == "closure_failed" and "表格" in page.reason


def test_a_cid_page_is_handed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from ragdemo.parse.local.layout import PageLayout, Word

    layout = PageLayout(1, 600, 800, [Word("(cid:12)", 0, 10, 0, 10)], "(cid:12) " + "x" * 80)
    monkeypatch.setattr(pipeline, "extract_pages", lambda _b: [layout])
    (page,) = parse_pages_deterministic(b"ignored")
    assert page.status == "no_text_layer" and "cid" in page.reason


@pytest.mark.xfail(
    strict=True,
    reason="已知限制（Opus 复核 I1，v1 不修）：两栏基线错位 7pt 时行聚类交错左右栏，"
    "行间空白带检测抓不到，'growth of 25.0%' 被接到右栏句子里；第二版 T3 整页空白带处理",
)
def test_two_columns_with_offset_baselines_known_limitation() -> None:
    left = ["Our chip segment revenue rose", "sharply this year, reaching",
            "growth of 25.0% versus last", "year on strong server demand."]  # fmt: skip
    right = ["The consumer segment fell as", "inventory digestion continued,",
             "a decline of 8.0% versus last", "year amid weak handset sales."]  # fmt: skip
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.setFont("Helvetica", 10)
    for i in range(4):
        c.drawString(50, 800 - 100 - i * 14, left[i])
        c.drawString(330, 800 - 100 - 7 - i * 14, right[i])
    c.showPage()
    c.save()
    (page,) = parse_pages_deterministic(buf.getvalue())
    # 期望：要么退回，要么 25.0% 与 chip segment 在同一段
    assert page.status != "ok" or "chip segment revenue rose sharply this year" in page.markdown


def _split_heading_pdf(value: str) -> bytes:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    y = 80.0
    body = "Revenue grew strongly during the period and management expects momentum"
    for n in (1, 2):
        for _ in range(3):
            c.setFont("Helvetica", 10)
            c.drawString(60, 800 - y, body)
            y += 15
        y += 10
        c.setFont("Helvetica-Bold", 10)
        c.drawString(60, 800 - y, f"{n}. Review period")
        c.setFont("STSong-Light", 10)
        c.drawString(300, 800 - y, value)
        y += 25
    for _ in range(3):
        c.setFont("Helvetica", 10)
        c.drawString(60, 800 - y, body)
        y += 15
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.parametrize("value", ["2.5 倍", "1,234.56 万元", "3.1 3.2"])
def test_a_split_off_heading_value_is_its_own_block_and_passes_the_self_checks(
    value: str,
) -> None:
    (page,) = parse_pages_deterministic(_split_heading_pdf(value))
    assert page.status == "ok", page.reason
    blocks = page.markdown.split("\n\n")
    assert "# 1. Review period" in blocks and "# 2. Review period" in blocks
    k = blocks.index("# 1. Review period")
    assert blocks[k + 1].replace(" ", "") == value.replace(" ", "")
    assert blocks[k + 2].startswith("Revenue grew")
    assert numbers_in(page.markdown) == numbers_in(page.source_text)
