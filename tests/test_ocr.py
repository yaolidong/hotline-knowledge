"""本地真实 OCR / PDF 渲染测试，以及原件访问权限与资源边界契约。"""

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import headers, import_ready
from hotline.documents import open_image, render_source, source_info
from hotline.models import ImportMetadata
from hotline.ocr import LocalOCR
from hotline.parsing import parse_document
from hotline.settings import ServiceError, Settings
from hotline.store import dumps
from scripts.create_ocr_fixtures import create_fixtures


@pytest.fixture(scope="module")
def scan_files(tmp_path_factory):
    try:
        return create_fixtures(tmp_path_factory.mktemp("scan-fixtures"))
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def local_ocr():
    return LocalOCR(Settings(_env_file=None))


def test_real_chinese_scan_pdf_and_mixed_pages(scan_files, local_ocr):
    scans = parse_document((scan_files / "synthetic-scan.pdf").read_bytes(), ".pdf", "scan", ocr=local_ocr)
    assert "测试申请表" in scans.text and "不得申请扫描登记" in scans.text
    assert {p["method"] for p in scans.metadata["_source"]["pages"]} == {"ocr"}
    assert {c.page for c in scans.chunks} == {1, 2}
    for chunk in scans.chunks:
        assert scans.text[chunk.start : chunk.end] == chunk.text
    for page in scans.metadata["_source"]["pages"]:
        for line in page["lines"]:
            assert scans.text[page["start"] + line["start"] : page["start"] + line["end"]] == line["text"]
            assert 0 <= line["confidence"] <= 1 and len(line["box"]) == 4
    mixed = parse_document((scan_files / "synthetic-mixed.pdf").read_bytes(), ".pdf", "mixed", ocr=local_ocr)
    assert [p["method"] for p in mixed.metadata["_source"]["pages"]] == ["native", "ocr"]
    assert "Native PDF text" in mixed.text and "测试服务局" in mixed.text


def test_real_image_ocr_and_disable(scan_files, local_ocr):
    data = (scan_files / "synthetic-scan.png").read_bytes()
    parsed = parse_document(data, ".png", "image", ocr=local_ocr)
    assert "五个工作日" in parsed.text and parsed.chunks[0].page == 1
    with pytest.raises(ServiceError, match="OCR"):
        parse_document(data, ".png", "image", ocr=local_ocr, ocr_mode="off")
    with pytest.raises(ServiceError, match="OCR"):
        parse_document(
            (scan_files / "synthetic-scan.pdf").read_bytes(), ".pdf", "off", ocr=local_ocr, ocr_mode="off"
        )


def test_ocr_force_and_page_budget(scan_files):
    class CountingOCR:
        # 本测试只验证路由和页数上限；识别质量由上面的真实模型测试覆盖。
        settings = Settings(_env_file=None, ocr_max_pages=2)
        calls = 0

        def recognize(self, image):
            self.calls += 1
            return "测试文字", {"method": "ocr", "lines": [], "warnings": []}

    engine = CountingOCR()
    data = (scan_files / "synthetic-mixed.pdf").read_bytes()
    parse_document(data, ".pdf", "force", ocr=engine, ocr_mode="force")
    assert engine.calls == 2
    engine.settings.ocr_max_pages = 1
    with pytest.raises(ServiceError, match="上限"):
        parse_document(data, ".pdf", "limited", ocr=engine, ocr_mode="force")


def test_low_confidence_is_preserved_for_review():
    engine = LocalOCR(Settings(_env_file=None))
    engine.engine = lambda image: SimpleNamespace(
        txts=["不得申请"], scores=[0.42], boxes=np.array([[[0, 0], [80, 0], [80, 20], [0, 20]]])
    )
    with Image.new("RGB", (100, 100)) as image:
        text, info = engine.recognize(image)
    assert text == "不得申请"
    assert info["low_confidence_lines"] == 1 and info["warnings"]


@pytest.mark.parametrize("extension", [".png", ".jpg", ".webp"])
def test_image_signatures_and_preview(scan_files, extension):
    data = (scan_files / ("synthetic-scan" + extension)).read_bytes()
    assert source_info(data, extension)["page_count"] == 1
    preview = render_source(data, extension, 1, 800)
    with Image.open(io.BytesIO(preview)) as image:
        assert max(image.size) == 800 and image.format == "PNG"
    with pytest.raises(ServiceError):
        open_image(b"invalid", extension)
    with pytest.raises(ServiceError):
        render_source(data, extension, 2, 800)


def test_pdf_render_concurrency_and_page_bounds(scan_files):
    data = (scan_files / "synthetic-scan.pdf").read_bytes()
    assert source_info(data, ".pdf")["page_count"] == 2
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda n: render_source(data, ".pdf", n, 800), [1, 2, 1, 2]))
    assert values[0] == values[2] and values[1] == values[3] and values[0] != values[1]
    for page in (0, 3):
        with pytest.raises(ServiceError, match="页码"):
            render_source(data, ".pdf", page, 800)


def test_preview_auth_upload_and_filters(application, scan_files):
    with TestClient(application) as client:
        data = (scan_files / "synthetic-scan.png").read_bytes()
        for role in ("agent", "reviewer"):
            assert (
                client.post(
                    "/api/import/files", headers=headers(role), files={"files": ("scan.png", data)}
                ).status_code
                == 403
            )
        assert (
            client.post(
                "/api/import/files", headers=headers(), files={"files": ("scan.jpg", data)}
            ).status_code
            == 400
        )
        response = client.post(
            "/api/import/files",
            headers=headers(),
            files={"files": ("scan.png", data)},
            data={"metadata": '{"synthetic":true}'},
        )
        assert response.status_code == 202
        rid = response.json()["items"][0]["revision_id"]
        for suffix in ("info", "pages/1", "text"):
            assert client.get(f"/api/source/{rid}/{suffix}").status_code == 401
            assert client.get(f"/api/source/{rid}/{suffix}", headers=headers("agent")).status_code == 403
            assert client.get(f"/api/source/{rid}/{suffix}", headers=headers("reviewer")).status_code == 200
        preview = client.get(f"/api/source/{rid}/pages/1", headers=headers())
        assert preview.content.startswith(b"\x89PNG") and preview.headers["cache-control"] == "no-store"
        assert client.get(f"/api/source/{rid}/pages/0", headers=headers()).status_code == 404
        assert client.get(f"/api/source/{rid}/pages/1?size=99999", headers=headers()).status_code == 422
        assert (
            client.get("/api/revisions?search=scan.png&state=RUNNING", headers=headers()).json()["total"] == 1
        )
        assert client.get("/api/revisions?search=' OR 1=1--", headers=headers()).json()["total"] == 0
        assert client.get("/api/revisions?state=unknown", headers=headers()).status_code == 422
        application.state.store.fail(rid, "权限测试到此结束，未执行知识抽取")
        ready = import_ready(client, application)
        drafts = client.get("/api/revisions?state=DRAFT", headers=headers()).json()
        assert [r["id"] for r in drafts["items"]] == [ready]


def test_legacy_dedup_fingerprint_preserved(application):
    meta = ImportMetadata(title="历史资料")
    result = application.state.store.ingest(b"old document", "old.txt", ".txt", meta, "tester")
    # 固定历史格式，不能随当前模型新增可选字段而改变旧指纹预期。
    old_metadata = {
        "title": "历史资料",
        "region": "",
        "document_number": "",
        "source": "",
        "document_type": "guide",
        "synthetic": False,
    }
    expected = hashlib.sha256(
        (hashlib.sha256(b"old document").hexdigest() + dumps(old_metadata)).encode()
    ).hexdigest()
    with application.state.store.connect() as db:
        assert (
            db.execute("SELECT fingerprint FROM documents WHERE id=?", (result["document_id"],)).fetchone()[0]
            == expected
        )
    again = application.state.store.ingest(b"old document", "old.txt", ".txt", meta, "tester")
    assert again["duplicate"] and result["revision_id"] == again["revision_id"]


def test_transparent_and_animated_images():
    buffer = io.BytesIO()
    with Image.new("RGBA", (10, 10), (0, 0, 0, 0)) as source:
        source.putpixel((5, 5), (0, 0, 0, 255))
        source.save(buffer, format="PNG")
    with open_image(buffer.getvalue(), ".png") as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((5, 5)) == (0, 0, 0)
    buffer = io.BytesIO()
    with Image.new("RGB", (10, 10), "white") as first, Image.new("RGB", (10, 10), "black") as second:
        first.save(buffer, format="WEBP", save_all=True, append_images=[second])
    with pytest.raises(ServiceError, match="多帧"):
        open_image(buffer.getvalue(), ".webp")
