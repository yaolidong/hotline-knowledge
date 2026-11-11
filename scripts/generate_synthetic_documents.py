"""从明确标记的合成指南生成 PDF/DOCX，用于真实导入回归。"""

from pathlib import Path

from docx import Document
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

source = Path("examples/synthetic_guide.txt").read_text(encoding="utf-8")
output = Path("data/validation/fixtures")
output.mkdir(parents=True, exist_ok=True)
doc = Document()
for line in source.splitlines():
    doc.add_paragraph(line)
doc.save(output / "synthetic_guide.docx")
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
canvas = Canvas(str(output / "synthetic_guide.pdf"))
canvas.setFont("STSong-Light", 10)
for i, line in enumerate(source.splitlines()):
    canvas.drawString(40, 780 - i * 30, line)
canvas.save()
print(f"已生成合成测试 PDF/DOCX：{output}")
