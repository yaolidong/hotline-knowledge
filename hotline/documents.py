"""原件渲染和图片校验；PDFium 的所有调用共享锁，不执行 PDF 脚本。"""

import io
import math
import re
import threading
import warnings
from contextlib import closing

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from hotline.settings import ServiceError

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MAX_IMAGE_PIXELS = 24_000_000
PDF_LOCK = threading.RLock()


def native_page_regions(data, page_number, text):
    """用 PDFium 精确查找唯一文字行，坐标转换与页面渲染共享 PDFium 规则。"""
    lines = []
    with PDF_LOCK, pdfium.PdfDocument(data) as pdf:
        with closing(pdf[page_number - 1]) as page, closing(page.get_textpage()) as textpage:
            width, height = page.get_size()
            if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                return lines
            # 虚拟渲染尺寸仅作坐标换算，不分配位图；处理裁剪框及页面旋转。
            side = 10000
            conversion = pdfium.PdfPosConv(page, (0, 0, side, side, 0))
            for match in list(re.finditer(r"[^\n]+", text))[:10000]:
                value = match.group().strip()
                if not value:
                    continue
                with closing(textpage.search(value, match_case=True)) as search:
                    hit = search.get_next()
                    if not hit or search.get_next() is not None:
                        continue
                # 多个视觉矩形各自保留，不使用一个大框覆盖无关内容。
                count = textpage.count_rects(*hit)
                for index in range(min(count, 100)):
                    left, bottom, right, top = textpage.get_rect(index)
                    points = [
                        conversion.to_bitmap(x, y)
                        for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
                    ]
                    if not all(0 <= n <= side for point in points for n in point):
                        continue
                    offset = match.start() + match.group().index(value)
                    lines.append(
                        {
                            "text": value,
                            "start": offset,
                            "end": offset + len(value),
                            "box": [[x / side, y / side] for x, y in points],
                            "confidence": None,
                        }
                    )
    return lines


def open_image(data: bytes, extension: str) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format != IMAGE_FORMATS.get(extension):
                    raise ServiceError("图片内容与扩展名不一致，仅支持 PNG、JPEG、WebP")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ServiceError("图片超过 2400 万像素，请缩小后上传")
                if getattr(source, "n_frames", 1) > 1:
                    raise ServiceError("暂不支持多帧图片，请拆为单页图片或 PDF")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    # 透明扫描底色按白纸合成，避免黑色正文与透明底混成黑块。
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    background.alpha_composite(rgba)
                    return background.convert("RGB")
                return oriented.convert("RGB")
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError("图片损坏、尺寸超限或格式不受支持") from exc


def pdf_info(data: bytes) -> dict:
    try:
        with PDF_LOCK, pdfium.PdfDocument(data) as pdf:
            if not 1 <= len(pdf) <= 500:
                raise ServiceError("PDF 页数必须在 1 至 500 页之间")
            return {"page_count": len(pdf), "kind": "pdf", "visual": True}
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError("PDF 损坏或已加密，无法打开原件") from exc


def render_pdf(data: bytes, page_number: int, max_side: int = 2200) -> Image.Image:
    try:
        with PDF_LOCK, pdfium.PdfDocument(data) as pdf:
            if not 1 <= page_number <= min(len(pdf), 500):
                raise ServiceError("页码超出文件范围", 404)
            with closing(pdf[page_number - 1]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ServiceError("PDF 页面尺寸不合法")
                scale = min(max_side / max(width, height), 4)
                with closing(page.render(scale=scale)) as bitmap:
                    # 脱离 PDFium 缓冲区后再释放锁，避免并发访问已释放的内存。
                    return bitmap.to_pil().convert("RGB").copy()
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError("PDF 页面渲染失败，请下载原件核对") from exc


def source_info(data: bytes, extension: str) -> dict:
    if extension == ".pdf":
        return pdf_info(data)
    if extension in IMAGE_FORMATS:
        with open_image(data, extension) as image:
            return {
                "page_count": 1,
                "kind": "image",
                "visual": True,
                "width": image.width,
                "height": image.height,
            }
    return {"page_count": 0, "kind": "text", "visual": False}


def render_source(data: bytes, extension: str, page_number: int, max_side: int) -> bytes:
    if extension == ".pdf":
        image = render_pdf(data, page_number, max_side)
    elif extension in IMAGE_FORMATS and page_number == 1:
        image = open_image(data, extension)
        image.thumbnail((max_side, max_side))
    else:
        raise ServiceError("该文件没有可预览的图像页面", 404)
    with image:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
