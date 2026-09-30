"""生成 tests/fixtures/pdf/table_sample.pdf：一段正文 + 一张 3x3 数字表。
只用 ASCII，避开 CID 字体在不同 pdfminer 版本下的抽取差异。产物提交进仓库，测试不依赖 reportlab。"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path("tests/fixtures/pdf/table_sample.pdf")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=A4)
    _, height = A4
    c.setFont("Helvetica", 11)
    c.drawString(60, height - 80, "Revenue grew steadily during the reporting period.")
    rows = [
        ("Item", "2024", "2023"),
        ("Revenue", "91.2", "60.1"),
        ("Net profit", "19.3", "12.7"),
    ]
    y = height - 140
    for label, a, b in rows:
        c.drawString(60, y, label)
        c.drawString(260, y, a)
        c.drawString(360, y, b)
        y -= 20
    c.showPage()
    c.save()


if __name__ == "__main__":
    main()
