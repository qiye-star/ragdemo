from pathlib import Path

from ragdemo.parse.local.layout import Word, extract_pages

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")


def test_extract_pages_returns_words_with_coordinates_and_text_layer() -> None:
    pages = extract_pages(FIXTURE.read_bytes())
    assert len(pages) == 1
    page = pages[0]
    assert page.page == 1 and page.width > 0 and page.height > 0
    texts = [w.text for w in page.words]
    assert "91.2" in texts and "60.1" in texts and "Revenue" in texts
    assert "91.2" in page.text and "19.3" in page.text


def test_words_on_the_same_table_row_share_a_vertical_band() -> None:
    page = extract_pages(FIXTURE.read_bytes())[0]
    by_text = {w.text: w for w in page.words}
    assert abs(by_text["91.2"].mid_y - by_text["60.1"].mid_y) < 3
    assert by_text["91.2"].x0 < by_text["60.1"].x0  # 2024 列在 2023 列左边


def test_a_non_pdf_raises_a_clear_error() -> None:
    import pytest

    with pytest.raises(ValueError, match="不是有效的 PDF"):
        extract_pages(b"not a pdf")


def test_word_mid_y() -> None:
    assert Word("a", 0, 1, 10, 20).mid_y == 15


def _pdf_with_fonts() -> bytes:
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.setFont("Helvetica-Bold", 20)
    c.drawString(60, 740, "Big Title")
    c.setFont("Helvetica", 10)
    c.drawString(60, 700, "plain body words here")
    c.save()
    return buf.getvalue()


def test_words_carry_font_size_and_bold_flag() -> None:
    page = extract_pages(_pdf_with_fonts())[0]
    by = {w.text: w for w in page.words}
    assert abs(by["Big"].size - 20) < 0.5 and by["Big"].bold
    assert abs(by["plain"].size - 10) < 0.5 and not by["plain"].bold


def test_word_font_fields_default_so_old_constructors_still_work() -> None:
    w = Word("a", 0, 1, 10, 20)
    assert w.size == 0.0 and w.bold is False


def test_font_info_does_not_change_word_segmentation() -> None:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(FIXTURE.read_bytes())) as pdf:
        plain = [str(w["text"]) for w in pdf.pages[0].extract_words(keep_blank_chars=False)]
    assert [w.text for w in extract_pages(FIXTURE.read_bytes())[0].words] == plain
