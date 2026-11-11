"""保留页码、规范文本偏移和条款层级；扫描页交给本地 OCR。"""

import io
import re
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass

from docx import Document
from docx.table import Table
from pypdf import PdfReader
import pdfplumber

from hotline.documents import IMAGE_FORMATS, open_image, render_pdf, native_page_regions
from hotline.models import Chunk, stable_id
from hotline.settings import ServiceError
from hotline.pdf_tables import extract_table_page

HEADING = re.compile(
    r"^(第[一二三四五六七八九十百千万零〇\d]+[章节条]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十\d]+[）)]|附件[：:\d一二三]|(?:申请条件|申请材料|办理流程|办理渠道|办理部门|政策依据|收费标准|办理时限)[：:]?)"
)


@dataclass
class Parsed:
    text: str
    chunks: list[Chunk]
    metadata: dict


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            if "\x00" in text:
                raise ServiceError("文本包含空字符，可能不是 TXT 文件")
            return text
        except UnicodeDecodeError:
            continue
    raise ServiceError("文本编码不受支持，请使用 UTF-8 或 GB18030")


def extract_pages(
    data: bytes,
    extension: str,
    max_chars: int,
    *,
    ocr=None,
    ocr_mode="auto",
    source_pages=None,
    progress=None,
) -> list[tuple[int | None, str]]:
    source_pages = source_pages if source_pages is not None else []
    if extension == ".txt":
        return [(None, decode_text(data))]
    with ExitStack() as resources:
        return _extract_pages(
            data,
            extension,
            max_chars,
            ocr=ocr,
            ocr_mode=ocr_mode,
            source_pages=source_pages,
            progress=progress,
            resources=resources,
        )


def _extract_pages(data, extension, max_chars, *, ocr, ocr_mode, source_pages, progress, resources):
    try:
        if extension in IMAGE_FORMATS:
            if ocr is None or ocr_mode == "off":
                raise ServiceError("扫描图片需要启用本地 OCR")
            if progress:
                progress(0, 1)
            with open_image(data, extension) as image:
                text, info = ocr.recognize(image)
            source_pages.append({"page": 1, **info})
            if progress:
                progress(1, 1)
            return [(1, text)]
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise ServiceError("PDF 文件签名不正确")
            pdf = PdfReader(io.BytesIO(data))
            if pdf.is_encrypted:
                raise ServiceError("不支持加密 PDF，请先提供解密文件")
            if len(pdf.pages) > 500:
                raise ServiceError("PDF 超过 500 页，请拆分后导入")
            layout = resources.enter_context(pdfplumber.open(io.BytesIO(data)))
            pages = []
            count = ocr_count = 0
            if progress:
                progress(0, len(pdf.pages))
            for index, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n")
                # 少量文字可能只是扫描页上的电子页眉；强制模式处理已有错误文字层的文件。
                page_resources = page.get("/Resources", {})
                page_resources = (
                    page_resources.get_object() if hasattr(page_resources, "get_object") else page_resources
                )
                objects = page_resources.get("/XObject", {})
                objects = objects.get_object() if hasattr(objects, "get_object") else objects
                has_image = any(obj.get_object().get("/Subtype") == "/Image" for obj in objects.values())
                needs_ocr = ocr_mode == "force" or not text.strip() or (has_image and len(text.strip()) < 80)
                info = {"method": "native", "warnings": [], "lines": []}
                if needs_ocr and ocr_mode != "off" and ocr is not None:
                    ocr_count += 1
                    if ocr_count > ocr.settings.ocr_max_pages:
                        raise ServiceError(f"扫描页超过 OCR 上限 {ocr.settings.ocr_max_pages} 页，请拆分文件")
                    with render_pdf(data, index, ocr.settings.ocr_render_side) as image:
                        text, info = ocr.recognize(image)
                elif not text.strip():
                    raise ServiceError(f"PDF 第 {index} 页无可提取文本，需要 OCR 或移除空白页后重试")
                if info["method"] == "native":
                    try:
                        structured = extract_table_page(layout.pages[index - 1])
                        if structured:
                            text, info = structured
                        else:
                            info["lines"] = native_page_regions(data, index, text)
                    except Exception:
                        info["warnings"].append("表格或文字区域未能定位，已保留文字层，请对照原件")
                source_pages.append({"page": index, **info})
                count += len(text)
                if count > max_chars:
                    raise ServiceError("解析文本超出长度限制")
                pages.append((index, text))
                if progress:
                    progress(index, len(pdf.pages))
            return pages
        if extension == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(x.file_size for x in entries) > 80 * 1024 * 1024:
                    raise ServiceError("DOCX 解压大小或成员数量超限")
                if "word/document.xml" not in archive.namelist():
                    raise ServiceError("不是有效 DOCX 文档")
            document = Document(io.BytesIO(data))
            blocks, cells, tables = [], [], []
            cursor = 0
            for block in document.iter_inner_content():
                if isinstance(block, Table):
                    table_id = len(tables) + 1
                    body = "[表格]\n"
                    # python-docx 将合并单元格映射到多个网格位置。
                    # 按底层单元格身份保存一次内容，不能按文字去重（相同文字可以属于不同格）。
                    origins = {}
                    for row_index, row in enumerate(block.rows, 1):
                        for col_index, cell in enumerate(row.cells, 1 + row.grid_cols_before):
                            if cell._tc in origins:
                                origin = origins[cell._tc]
                                origin["row_span"] = max(origin["row_span"], row_index - origin["row"] + 1)
                                origin["column_span"] = max(
                                    origin["column_span"], col_index - origin["column"] + 1
                                )
                                value = ""
                            else:
                                value = cell.text.replace("\r\n", "\n").replace("\r", "\n")
                                left = cursor + len(body)
                                origin = {
                                    "table": table_id,
                                    "row": row_index,
                                    "column": col_index,
                                    "row_span": 1,
                                    "column_span": 1,
                                    "text": value,
                                    "start": left,
                                    "end": left + len(value),
                                }
                                origins[cell._tc] = origin
                                cells.append(origin)
                            body += value + (
                                " | " if col_index < len(row.cells) + row.grid_cols_before else "\n"
                            )
                    body += "[/表格]"
                    tables.append({"id": table_id, "start": cursor, "end": cursor + len(body)})
                    blocks.append(body)
                else:
                    blocks.append(block.text.replace("\r\n", "\n").replace("\r", "\n"))
                cursor += len(blocks[-1]) + 2
            source_pages.append(
                {
                    "page": None,
                    "method": "docx",
                    "lines": [],
                    "cells": cells,
                    "tables": tables,
                    "warnings": [],
                }
            )
            return [(None, "\n\n".join(blocks))]
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError("文件损坏或格式不受支持") from exc
    raise ServiceError("仅支持 PDF、DOCX、TXT、PNG、JPEG、WebP")


def parse_document(
    data: bytes,
    extension: str,
    revision_id: str,
    max_chars=500000,
    max_chunks=300,
    *,
    ocr=None,
    ocr_mode="auto",
    progress=None,
) -> Parsed:
    source_pages = []
    pages = extract_pages(
        data, extension, max_chars, ocr=ocr, ocr_mode=ocr_mode, source_pages=source_pages, progress=progress
    )
    # 偏移针对下面保存的规范文本，不宣称是 PDF 字节或版面坐标。
    pages = [(page, text.replace("\r\n", "\n").replace("\r", "\n")) for page, text in pages]
    text = "\n\n".join(content for _, content in pages)
    if not text.strip() or len(text) > max_chars:
        raise ServiceError("文档为空或文本长度超限")
    chunks = []
    base = 0
    chapter = section = provision = ""
    for page, content in pages:
        spans = []
        start = 0
        in_table = False
        for match in re.finditer(r"[^\n]*(?:\n|$)", content):
            line = match.group().strip()
            if not line:
                continue
            if line == "[表格]":
                if match.start() > start:
                    spans.append((start, match.start()))
                start = match.start()
                in_table = True
            elif line == "[/表格]":
                spans.append((start, match.end()))
                start = match.end()
                in_table = False
            elif not in_table and HEADING.match(line) and match.start() > start:
                spans.append((start, match.start()))
                start = match.start()
        if start < len(content):
            spans.append((start, len(content)))
        for lo, hi in spans:
            raw = content[lo:hi]
            first = raw.strip().split("\n")[0]
            if re.match(r"^第.+章", first):
                chapter, section, provision = first, "", ""
            elif re.match(r"^第.+节", first):
                section, provision = first, ""
            elif HEADING.match(first):
                provision = first[:150]
            # 超长条款按段落/句子边界拆分，并保留共同条款和前后块关联。
            cuts = [lo]
            if len(raw) > 6000:
                if "[表格]" in raw:
                    raise ServiceError("单个表格超过 6000 字，请先拆分表格，避免丢失表头")
                cursor = lo
                for boundary in re.finditer(r"[。！？；]\s*|\n\s*\n", raw):
                    end = lo + boundary.end()
                    if end - cursor >= 3000:
                        cuts.append(end)
                        cursor = end
            cuts.append(hi)
            for left, right in zip(cuts, cuts[1:]):
                value = content[left:right]
                if not value.strip():
                    continue
                if len(value) > 12000:
                    raise ServiceError("单段文本过长且没有语义边界，请整理段落后导入")
                chunks.append(
                    Chunk(
                        id=stable_id(revision_id, str(base + left)),
                        text=value,
                        start=base + left,
                        end=base + right,
                        page=page,
                        section=" / ".join(x for x in (chapter, section, provision) if x),
                    )
                )
        base += len(content) + 2
    if len(chunks) > max_chunks:
        raise ServiceError("文本块数量超限，请拆分文件")
    for index, chunk in enumerate(chunks):
        chunk.previous = chunks[index - 1].id if index else None
        chunk.following = chunks[index + 1].id if index + 1 < len(chunks) else None
    metadata = (
        {"_source": {"version": 2, "pages": source_pages, "ocr_mode": ocr_mode}} if source_pages else {}
    )
    offset = 0
    for info, (_, content) in zip(source_pages, pages):
        info["start"], info["end"] = offset, offset + len(content)
        offset += len(content) + 2
    fields = {
        "issued_on": "发布日期|印发日期",
        "effective_on": "施行日期|生效日期",
        "expires_on": "失效日期|有效期至",
        "repealed_on": "废止日期",
        "issuer": "发布机关|发文机关",
        "document_number": "发文字号|文号",
    }
    for key, label in fields.items():
        found = re.search(rf"(?P<label>{label})[：:][ \t]*([^\n]+)", text)
        if found:
            metadata[key] = {
                "value": found.group(2).strip(),
                "start": found.start(2),
                "end": found.end(2),
                "label": found.group("label"),
            }
    return Parsed(text, chunks, metadata)
