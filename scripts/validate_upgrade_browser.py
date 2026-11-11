"""真实浏览器检查语义审核、表格定位、PDF/OCR 框及可撤销身份关联。无接口 Mock。"""

import io
import json
import os
import time
from pathlib import Path

import httpx
from docx import Document
from playwright.sync_api import sync_playwright
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from hotline.settings import Settings


def main():
    settings = Settings()
    if not settings.namespace.startswith("HotlineTest"):
        raise SystemExit("只允许测试项目")
    token = next(k for k, u in settings.users.items() if u.role == "admin")
    output = Path("test-results/semantic-upgrade")
    progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    errors, checks = [], {}
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        trust_env=False,
        timeout=120,
        headers={"Authorization": "Bearer " + token},
    ) as client:

        def get(path):
            r = client.get(path)
            r.raise_for_status()
            return r.json()

        def ready(rid):
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                r = get(f"/api/revisions/{rid}")
                if r["task_status"] == "READY":
                    return r
                if r["task_status"] == "FAILED":
                    raise RuntimeError(r["error"])
                time.sleep(2)
            raise RuntimeError("任务等待超时")

        def upload(data, name):
            response = client.post(
                "/api/import/files",
                files={"files": (name, data)},
                data={
                    "metadata": json.dumps(
                        {"region": "测试甲区", "synthetic": True, "title": name}, ensure_ascii=False
                    )
                },
            )
            response.raise_for_status()
            return ready(response.json()["items"][0]["revision_id"])

        doc = Document()
        doc.add_paragraph("合成回归文件，非真实政策。事项名称：测试材料核验申请。办理部门：测试核验服务局。")
        table = doc.add_table(rows=3, cols=2)
        for row, values in zip(
            table.rows, [("材料名称", "份数"), ("测试申请表", "一份"), ("测试补充表", "一份")]
        ):
            for cell, value in zip(row.cells, values):
                cell.text = value
        buf = io.BytesIO()
        doc.save(buf)
        docx = upload(buf.getvalue(), "合成表格区域定位与身份关联.docx")
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        buf = io.BytesIO()
        canvas = Canvas(buf, invariant=1)
        canvas.setFont("STSong-Light", 14)
        canvas.drawString(40, 750, "合成回归资料，非真实政策。")
        canvas.drawString(40, 720, "事项名称：测试原生页面核验。办理部门：测试服务局。")
        canvas.save()
        pdf = upload(buf.getvalue(), "合成原生PDF区域定位.pdf")
        print("DOCX 与原生 PDF 已完成真实解析和模型抽取。", flush=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.getenv(
                    "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
                ),
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://127.0.0.1:8080")
            page.locator("#token").fill(token)
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")
            page.locator('nav button[data-tab="review"]').click()
            page.evaluate("rid => loadRevision(rid, 'review')", progress["public_draft"])
            assert page.locator("select[data-quality-issue]").count() > 0
            page.screenshot(path=str(output / "review.png"), full_page=True)
            checks["quality_review"] = True
            # 重复的一份必须只定位第三行第二列，原始表头与相邻行同时可见。
            chunk = next(c for c in docx["chunks"] if "[/表格]" in c["text"])
            start = chunk["text"].rindex("一份")
            evidence = {"chunk_id": chunk["id"], "quote": "一份", "start": start, "end": start + 2}
            page.evaluate("a => openEvidence(a.evidence, a.rid)", {"evidence": evidence, "rid": docx["id"]})
            page.locator("dialog .reader-text table mark").wait_for()
            assert page.locator("dialog .reader-text table mark").count() == 1
            assert "第 3 行，第 2 列" in page.locator("dialog .reader-text table td:has(mark)").get_attribute(
                "title"
            )
            page.screenshot(path=str(output / "docx-cell.png"), full_page=True)
            page.get_by_role("button", name="关闭", exact=True).click()
            checks["docx_exact_cell"] = True
            for kind, record in (
                ("native-pdf", pdf),
                ("ocr-pdf", get("/api/revisions/fd371e6728214a998b606a194bed67a0")),
            ):
                chunk = record["chunks"][0]
                evidence = {
                    "chunk_id": chunk["id"],
                    "quote": chunk["text"],
                    "start": 0,
                    "end": len(chunk["text"]),
                }
                page.evaluate(
                    "a => openEvidence(a.evidence, a.rid)", {"evidence": evidence, "rid": record["id"]}
                )
                page.locator("dialog .evidence-box").first.wait_for()
                assert page.locator("dialog .evidence-box").count() > 0
                page.screenshot(path=str(output / f"{kind}.png"), full_page=True)
                page.get_by_role("button", name="关闭", exact=True).click()
                checks[kind] = True
            # 在真实审核页确认合成资料中的同一事项，然后撤销；不发布 DOCX 草稿。
            page.evaluate("rid => loadRevision(rid, 'review')", docx["id"])
            item = (
                page.locator(".record-card details")
                .filter(has=page.locator("summary", has_text="政务事项 · 测试材料核验申请"))
                .first
            )
            item.locator("summary").first.click()
            item.get_by_role("button", name="跨文档实体关联", exact=True).click()
            panel = item.locator(".identity-panel")
            panel.get_by_label("关联审核依据").fill(
                "合成回归：同地区同事项名称核对，仅关联身份，不合并要求。"
            )
            panel.get_by_role("button", name="确认同一实体", exact=True).first.click()
            panel.get_by_role("button", name="撤销关联", exact=True).first.wait_for()
            page.screenshot(path=str(output / "identity.png"), full_page=True)
            panel.get_by_label("关联审核依据").fill("合成回归：验证撤销，保留全部原始事实。")
            panel.get_by_role("button", name="撤销关联", exact=True).first.click()
            panel.get_by_text("已撤销", exact=False).wait_for()
            assert get(f"/api/revisions/{docx['id']}")["knowledge_status"] == "DRAFT"
            checks["identity_confirm_revoke"] = True
            assert not errors, errors
            browser.close()
    result = {
        "status": "PASS",
        "checks": checks,
        "browser_errors": errors,
        "docx": docx["id"],
        "pdf": pdf["id"],
        "public_draft": progress["public_draft"],
        "synthetic_published": report["synthetic"]["id"],
    }
    (output / "browser-report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
