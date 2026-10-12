from collections.abc import Sequence
from pathlib import Path

from ragdemo.parse.local.pipeline import _needs_handback, parse_pages_local
from ragdemo.parse.local.structure import ClosureFailed, StructureFailed

FIXTURE = Path("tests/fixtures/pdf/table_sample.pdf")


class EchoStructurer:
    """不调模型：把正文原样加上表格，模拟一个守规矩的大模型。"""

    def __init__(self) -> None:
        self.tables_seen: list[Sequence[str]] = []

    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        self.tables_seen.append(table_markdowns)
        return page_text + "\n\n" + "\n\n".join(table_markdowns)


class InventingStructurer:
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        raise ClosureFailed(["999.9"])


def test_a_text_page_with_a_table_is_ok_and_the_table_comes_from_coordinates() -> None:
    s = EchoStructurer()
    (page,) = parse_pages_local(FIXTURE.read_bytes(), s)
    assert page.status == "ok" and page.page == 1
    assert "| Revenue | 91.2 | 60.1 |" in page.markdown
    assert len(s.tables_seen[0]) == 1  # 表格由坐标还原后传给大模型，而不是让它猜


class BrokenStructurer:
    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        raise StructureFailed("回复被截断（finish_reason=length）")


def test_a_truncated_or_empty_reply_is_recorded_as_structure_failed() -> None:
    (page,) = parse_pages_local(FIXTURE.read_bytes(), BrokenStructurer())
    assert page.status == "structure_failed" and page.markdown == "" and page.violations == []
    assert page.reason == "回复被截断（finish_reason=length）"


def test_a_failed_closure_is_recorded_not_raised() -> None:
    (page,) = parse_pages_local(FIXTURE.read_bytes(), InventingStructurer())
    assert page.status == "closure_failed" and page.violations == ["999.9"]
    assert page.markdown == ""
    assert "999.9" in page.reason and "数字闭合失败" in page.reason


def test_a_page_without_a_text_layer_is_handed_back(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=595, height=842)  # 没有任何文字层，相当于扫描件
    path = tmp_path / "blank.pdf"
    with path.open("wb") as fh:
        w.write(fh)
    (page,) = parse_pages_local(path.read_bytes(), EchoStructurer())
    assert page.status == "no_text_layer" and page.markdown == ""
    assert page.reason == "文字层不足 40 字符"


class RecordingStructurer:
    def __init__(self) -> None:
        self.calls = 0

    def structure(self, page_text: str, table_markdowns: Sequence[str]) -> str:
        self.calls += 1
        return page_text


def test_a_scanned_page_with_only_a_stray_word_is_handed_back_not_ok() -> None:
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.drawString(280, 30, "12")  # 扫描件上仅有的页码文字层
    c.save()
    s = RecordingStructurer()
    (page,) = parse_pages_local(buf.getvalue(), s)
    assert page.status == "no_text_layer" and page.markdown == ""
    assert s.calls == 0


def test_ok_page_has_empty_reason() -> None:
    (page,) = parse_pages_local(FIXTURE.read_bytes(), EchoStructurer())
    assert page.reason == ""


def test_needs_handback_decision() -> None:
    assert _needs_handback("短") == "文字层不足 40 字符"
    assert _needs_handback("正常文字" * 20 + "(cid:123)") == "文字层乱码（cid）"
    assert _needs_handback("正常文字" * 20) is None


def test_needs_handback_is_public_and_the_old_name_is_an_alias() -> None:
    from ragdemo.parse.local.pipeline import needs_handback

    assert needs_handback is _needs_handback
    assert needs_handback("正常文字" * 20) is None
