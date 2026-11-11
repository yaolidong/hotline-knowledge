"""生成明确标记的合成扫描测试件；不是实际政策或政府发布原件。"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas


def create_fixtures(output=Path("test-results/ocr")):
    output.mkdir(parents=True, exist_ok=True)
    candidates = [
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    font_path = next((p for p in candidates if p.exists()), None)
    if not font_path:
        raise RuntimeError("生成中文合成测试件需要宋体或 NotoSansCJK 字体")
    font = ImageFont.truetype(str(font_path), 38)
    lines = [
        "合成测试资料，不是真实政策，不得用于实际办理。",
        "测试甲区扫描登记办事指南",
        "第一条 事项名称：扫描登记。",
        "办理部门：测试服务局。",
        "第二条 申请材料：测试申请表。",
        "第三条 申请条件：年满十八周岁，居住在测试甲区。",
        "第四条 办理流程：提交申请，然后窗口核验。",
        "第五条 办理渠道：测试线上平台。",
        "第六条 办理时限：五个工作日。",
    ]
    second = [
        "合成测试资料，不是真实政策，不得用于实际办理。",
        "扫描登记办事指南（续页）",
        "第七条 收费标准：不收费。",
        "第八条 排除条件：失信申请人不得申请扫描登记。",
    ]
    pages = []
    for content in (lines, second):
        image = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(content):
            draw.text((100, 160 + i * 100), line, font=font, fill="#111111")
        pages.append(image)
    pages[0].save(output / "synthetic-scan.png")
    pages[0].save(output / "synthetic-scan.jpg", quality=94)
    pages[0].save(output / "synthetic-scan.webp", quality=94)
    pages[0].save(output / "synthetic-scan.pdf", save_all=True, append_images=pages[1:], resolution=200)
    # 一页文字层、一页扫描图，用于确保自动 OCR 只处理需要识别的页面。
    native = io.BytesIO()
    canvas = Canvas(native)
    canvas.drawString(40, 700, "Synthetic testing only. Native PDF text is preserved without OCR.")
    canvas.drawString(40, 675, "This document contains no actual government policy. Second page is an image.")
    canvas.save()
    writer = PdfWriter()
    writer.append(PdfReader(native))
    writer.add_page(PdfReader(output / "synthetic-scan.pdf").pages[0])
    writer.write(output / "synthetic-mixed.pdf")
    (output / "expected.txt").write_text("\n".join(lines + second), encoding="utf-8")
    for image in pages:
        image.close()
    return output


if __name__ == "__main__":
    print(create_fixtures().resolve())
