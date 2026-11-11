"""仅下载清单中的公开政府文件，保留 URL、时间、SHA256 与原始字节；不自动审核发布。"""

import hashlib
import argparse
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

from hotline.parsing import parse_document
from hotline.settings import ServiceError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reader-text", action="store_true", help="直连失败时通过公开 Reader 获取官网正文；不会冒充原始 PDF"
    )
    parser.add_argument("--manifest", type=Path, default=Path("examples/public_policy_sources.json"))
    parser.add_argument("--output", type=Path, default=Path("data/public-policies"))
    parser.add_argument(
        "--download-only", action="store_true", help="仅获取并校验原件；OCR/抽取交给实际导入链路"
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
        for source in manifest:
            assert urlparse(source["url"]).hostname.endswith(".gov.cn")
            if Path(source["filename"]).name != source["filename"]:
                raise ValueError("清单文件名不能包含目录")
            path = output / source["filename"]
            extension = path.suffix.lower()
            if extension not in (".pdf", ".docx"):
                raise ValueError("清单只支持 PDF 和 DOCX 原件")
            report = {**source, "downloaded_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
            report["acquisition"] = "LOCAL_CACHE_RECHECK" if path.exists() else "DIRECT_OFFICIAL_DOWNLOAD"
            try:
                if not path.exists():
                    with client.stream("GET", source["url"]) as response:
                        response.raise_for_status()
                        parts, size = [], 0
                        for part in response.iter_bytes():
                            size += len(part)
                            if size > 20 * 1024 * 1024:
                                raise ValueError("文件超过 20 MB")
                            parts.append(part)
                        data = b"".join(parts)
                else:
                    data = path.read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                if source.get("sha256") and sha256 != source["sha256"]:
                    raise ValueError("原件哈希与固定评测清单不符，请核对官网是否更新文件")
                if extension == ".pdf":
                    if not data.startswith(b"%PDF-"):
                        raise ValueError("下载内容不是 PDF")
                    reader = PdfReader(io.BytesIO(data))
                    report.update(
                        pages=len(reader.pages),
                        page_text_chars=[len(p.extract_text() or "") for p in reader.pages],
                    )
                else:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        if "word/document.xml" not in archive.namelist():
                            raise ValueError("下载内容不是 DOCX")
                if not path.exists():
                    path.write_bytes(data)
                report.update(
                    bytes=len(data),
                    sha256=sha256,
                )
                if args.download_only:
                    report["parse_status"] = "DOWNLOADED"
                    reports.append(report)
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                    continue
                try:
                    parsed = parse_document(data, extension, source["id"])
                    report.update(parse_status="PARSED", chunks=len(parsed.chunks))
                    (output / (source["id"] + ".txt")).write_text(parsed.text, encoding="utf-8")
                except ServiceError as exc:
                    report.update(
                        parse_status="REQUIRES_OCR" if "OCR" in str(exc) else "FAILED", error=str(exc)
                    )
            except Exception as exc:
                report.update(parse_status="DOWNLOAD_FAILED", error=type(exc).__name__)
                if args.reader_text:
                    # 只发送清单中的公开政策 URL，不向第三方发送本地文档或凭据。
                    source_url = source["source_page"]
                    reader_url = "https://r.jina.ai/" + source_url
                    try:
                        response = client.get(reader_url)
                        response.raise_for_status()
                        raw = response.text
                        (output / (source["id"] + ".reader.txt")).write_text(raw, encoding="utf-8")
                        content = raw.partition("Markdown Content:")[2].strip()
                        if not content:
                            raise ValueError("正文为空")
                        text_path = output / (source["id"] + ".txt")
                        text_path.write_text(content, encoding="utf-8")
                        parsed = parse_document(content.encode("utf-8"), ".txt", source["id"])
                        report.update(
                            parse_status="DERIVED_TEXT",
                            local_text=str(text_path),
                            acquisition="公开 Reader 提取官网正文，非原始 PDF 下载",
                            reader_url=reader_url,
                            text_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                            chunks=len(parsed.chunks),
                            text_chars=len(content),
                            original_pdf_downloaded=False,
                            page_numbers_available=False,
                        )
                    except Exception as reader_error:
                        report["reader_error"] = type(reader_error).__name__
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    (output / "manifest.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
