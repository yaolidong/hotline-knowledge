import io
import zipfile

import pytest
from docx import Document
from pypdf import PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hotline.parsing import parse_document
from hotline.settings import ServiceError


def test_text_structure_offsets_metadata():
    text = "合成测试资料\n发布日期：2026年1月2日\n第一章 总则\n第一条 申请事项。\n（一）申请条件\n同时满足 A 和 B。\n第二条 材料清单。"
    result = parse_document(text.encode(), ".txt", "r")
    assert len(result.chunks) >= 4
    assert result.metadata["issued_on"]["value"] == "2026年1月2日"
    for chunk in result.chunks:
        assert result.text[chunk.start : chunk.end] == chunk.text
    assert "第一章" in result.chunks[-1].section
    assert result.chunks[0].following == result.chunks[1].id
    assert result.chunks[-1].previous == result.chunks[-2].id


def test_docx_preserves_table_order():
    doc = Document()
    doc.add_paragraph("第一条 合成测试指南")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "材料名称"
    table.cell(0, 1).text = "份数"
    table.cell(1, 0).text = "测试申请表"
    table.cell(1, 1).text = "一份"
    doc.add_paragraph("第二条 办理部门")
    buf = io.BytesIO()
    doc.save(buf)
    result = parse_document(buf.getvalue(), ".docx", "r")
    assert result.text.index("第一条") < result.text.index("测试申请表") < result.text.index("第二条")
    table_chunk = next(c for c in result.chunks if "测试申请表" in c.text)
    assert "材料名称" in table_chunk.text and "一份" in table_chunk.text
    assert table_chunk.page is None


def test_pdf_chinese_pages_and_offsets():
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    canvas = Canvas(buf)
    for text in ("第一条 合成测试资料", "第二条 申请材料为测试表格"):
        canvas.setFont("STSong-Light", 12)
        canvas.drawString(50, 750, text)
        canvas.showPage()
    canvas.save()
    result = parse_document(buf.getvalue(), ".pdf", "r")
    assert [c.page for c in result.chunks] == [1, 2]
    assert "申请材料" in result.chunks[1].text
    assert result.text[result.chunks[1].start : result.chunks[1].end] == result.chunks[1].text


def test_docx_merged_cells_keep_one_origin_and_distinct_equal_cells():
    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).merge(table.cell(1, 1)).text = "跨行跨列的办理条件"
    table.cell(0, 2).text = "同名材料"
    table.cell(1, 2).text = "同名材料"
    table.cell(2, 0).text = "后续材料"
    buf = io.BytesIO()
    doc.save(buf)
    parsed = parse_document(buf.getvalue(), ".docx", "r")
    assert parsed.text.count("跨行跨列的办理条件") == 1
    assert parsed.text.count("同名材料") == 2
    cells = parsed.metadata["_source"]["pages"][0]["cells"]
    origin = next(c for c in cells if c["text"] == "跨行跨列的办理条件")
    assert (origin["row"], origin["column"], origin["row_span"], origin["column_span"]) == (1, 1, 2, 2)
    assert len(cells) == 6
    for cell in cells:
        assert parsed.text[cell["start"] : cell["end"]] == cell["text"]
    for chunk in parsed.chunks:
        assert parsed.text[chunk.start : chunk.end] == chunk.text


def test_pdf_without_text_requires_ocr():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(ServiceError, match="OCR"):
        parse_document(buf.getvalue(), ".pdf", "r")


def test_pdf_tables_keep_cells_and_surrounding_text():
    from reportlab.platypus import Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    canvas = Canvas(buf)
    canvas.setFont("STSong-Light", 12)
    canvas.drawString(50, 800, "合成表格验证，不是真实政策")
    table = Table(
        [["材料清单", ""], ["材料名称", "份数"], ["测试申请表", "1份"]],
        colWidths=[180, 120],
        rowHeights=[30, 30, 30],
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ("SPAN", (0, 0), (1, 0)),
                ("GRID", (0, 0), (-1, -1), 1, (0, 0, 0)),
            ]
        )
    )
    table.wrapOn(canvas, 300, 90)
    table.drawOn(canvas, 50, 650)
    canvas.drawString(50, 600, "办理部门：测试服务局")
    canvas.showPage()
    canvas.setFont("STSong-Light", 12)
    canvas.drawString(50, 800, "第二页文字")
    table.drawOn(canvas, 50, 650)
    single = Table([["办理条件", "仅为合成测试的完整条件"]], colWidths=[100, 220], rowHeights=[50])
    single.setStyle(
        TableStyle([("FONTNAME", (0, 0), (-1, -1), "STSong-Light"), ("GRID", (0, 0), (-1, -1), 1, (0, 0, 0))])
    )
    single.wrapOn(canvas, 320, 50)
    single.drawOn(canvas, 50, 500)
    canvas.save()
    result = parse_document(buf.getvalue(), ".pdf", "r")
    assert "办理部门：测试服务局" in result.text
    for page in result.metadata["_source"]["pages"]:
        assert len(page["tables"]) == (1 if page["page"] == 1 else 2)
        assert len(page["cells"]) == (5 if page["page"] == 1 else 7)
        assert page["cells"][0]["column_span"] == 2
        for cell in page["cells"]:
            assert result.text[page["start"] + cell["start"] : page["start"] + cell["end"]] == cell["text"]
            assert all(0 <= n <= 1 for point in cell["box"] for n in point)
    assert all(result.text[c.start : c.end] == c.text for c in result.chunks)


@pytest.mark.parametrize(
    "data,extension",
    [(b"bad", ".pdf"), (b"bad", ".docx"), (b"", ".txt"), (b"\x00", ".txt"), (b"abc", ".exe")],
)
def test_invalid_files(data, extension):
    with pytest.raises(ServiceError):
        parse_document(data, extension, "r")


def test_gb18030_and_length_limit():
    assert "合成" in parse_document("合成测试".encode("gb18030"), ".txt", "r").text
    with pytest.raises(ServiceError, match="长度"):
        parse_document(b"a" * 101, ".txt", "r", max_chars=100)


def test_zip_bomb_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (81 * 1024 * 1024))
    with pytest.raises(ServiceError, match="解压大小"):
        parse_document(buf.getvalue(), ".docx", "r")
