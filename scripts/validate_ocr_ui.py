"""真实服务验收：扫描 PDF 导入、原件阅读、审核、Neo4j 图谱与 DeepSeek QA。

仅在独立 HotlineTest 项目中使用。默认不发布；--publish-reviewed 要求已有人工修正审计。
"""

import argparse
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish-reviewed", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest"), "只允许独立测试项目"
    tokens = {u.role: key for key, u in settings.users.items()}
    output = Path("test-results/ocr-ui")
    output.mkdir(parents=True, exist_ok=True)
    imported = json.loads(Path("test-results/ocr/live-import.json").read_text())
    rid = imported["items"][0]["revision_id"]
    base = "http://127.0.0.1:8080"
    with (
        httpx.Client(
            base_url=base, headers={"Authorization": "Bearer " + tokens["admin"]}, trust_env=False, timeout=30
        ) as client,
        sync_playwright() as playwright,
    ):
        record = client.get(f"/api/revisions/{rid}").json()
        assert record["task_status"] == "READY" and record["metadata"]["synthetic"]
        title = record["metadata"]["title"]
        errors, console_errors, failed_responses = [], [], []
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=os.getenv(
                "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
            ),
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: failed_responses.append((r.status, r.url)) if r.status >= 400 else None)
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        def login(role):
            page.goto(base)
            page.locator("#token").fill(tokens[role])
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")

        login("admin")
        for name, width, height in [("desktop", 1440, 1100), ("tablet", 820, 1180), ("mobile", 390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(output / f"query-{name}.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.locator('[data-tab="import"]').click()
        expect(page.locator("#service-status")).to_contain_text("本地 OCR")
        page.locator("#files").set_input_files("test-results/ocr/synthetic-scan.pdf")
        for name in ("title", "region", "source"):
            page.locator(f'#import-form [name="{name}"]').fill(record["metadata"][name])
        page.locator('#import-form [name="synthetic"]').check()
        page.screenshot(path=str(output / "import-desktop.png"), full_page=True)
        with page.expect_response(lambda r: r.url.endswith("/api/import/files")) as response:
            page.locator('#import-form button[type="submit"]').click()
        assert response.value.json()["items"][0]["revision_id"] == rid
        assert response.value.json()["items"][0]["duplicate"]
        page.locator("#import-result button").first.click()
        viewer = page.locator("#extraction-detail .source-viewer")
        viewer.locator("svg image").wait_for()
        canvas = viewer.locator(".reader-stage svg")
        original = canvas.get_attribute("viewBox")
        viewer.get_by_role("button", name="放大", exact=True).click()
        assert canvas.get_attribute("viewBox") != original
        expect(viewer.locator("output")).to_have_text("125%")
        viewer.get_by_role("button", name="缩小", exact=True).click()
        expect(viewer.locator("output")).to_have_text("100%")
        bounds = canvas.bounding_box()
        before = canvas.get_attribute("viewBox")
        page.mouse.move(bounds["x"] + 100, bounds["y"] + 100)
        page.mouse.down()
        page.mouse.move(bounds["x"] + 140, bounds["y"] + 140, steps=5)
        page.mouse.up()
        assert canvas.get_attribute("viewBox") != before
        viewer.get_by_role("button", name="旋转", exact=True).click()
        assert canvas.locator("g").first.get_attribute("transform") == "rotate(90)"
        viewer.get_by_role("button", name="下一页", exact=True).click()
        expect(viewer.locator(".reader-status")).to_contain_text("第 2 / 2 页")
        viewer.get_by_role("button", name="显示识别框", exact=True).click()
        assert viewer.locator(".ocr-box").count() >= 3
        viewer.get_by_role("button", name="识别 / 解析文字", exact=True).click()
        expect(viewer.locator(".reader-text")).to_contain_text("失信申请人不得申请扫描登记")
        with page.expect_download() as download:
            viewer.get_by_role("button", name="下载原文件", exact=True).click()
        download.value.save_as(output / "downloaded-original.pdf")
        assert (output / "downloaded-original.pdf").read_bytes() == Path(
            "test-results/ocr/synthetic-scan.pdf"
        ).read_bytes()
        page.locator('[data-tab="review"]').click()
        page.locator('#review-filter [name="state"]').select_option(record["knowledge_status"])
        page.locator('#review-filter [name="search"]').fill(title)
        page.locator("#review-filter button").click()
        page.locator("#review-list tbody tr").filter(has_text=title).first.get_by_role(
            "button", name="对照审核"
        ).click()
        review = page.locator("#review-detail")
        review.locator("svg image").wait_for()
        review.locator(".record-card details").filter(
            has=page.locator("summary", has_text="政务事项 · 扫描登记")
        ).first.locator("summary").first.click()
        review.get_by_role("button", name="定位原文", exact=True).first.click()
        expect(review.locator(".reader-status")).to_contain_text("第 1 / 2 页")
        review.locator(".evidence-box").first.wait_for(state="attached")
        for name, width, height in [("desktop", 1440, 1100), ("tablet", 820, 1180), ("mobile", 390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            review.scroll_into_view_if_needed()
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(output / f"review-{name}.png"))
        page.set_viewport_size({"width": 1440, "height": 1100})
        if args.publish_reviewed and record["knowledge_status"] != "PUBLISHED":
            history = client.get(f"/api/revisions/{rid}/history").json()["items"]
            assert any(item["action"] == "CORRECT" and "合成OCR验收" in item["note"] for item in history), (
                "必须先对照原件完成修正"
            )
            assert len(record["extraction"]["entities"]) == 7
            for action, expected in [("审核通过", "APPROVED"), ("发布到图谱", "PUBLISHED")]:
                if record["knowledge_status"] == "APPROVED" and action == "审核通过":
                    continue
                review.get_by_label("审核说明", exact=True).fill(
                    "合成OCR浏览器验收：已逐页核对原件、材料、条件及排除规则；仅发布至独立测试项目。"
                )
                with page.expect_response(
                    lambda r: r.url.endswith(f"/api/revisions/{rid}/review"), timeout=150000
                ) as response:
                    review.get_by_role("button", name=action, exact=True).click()
                assert response.value.status == 200, response.value.text()
                record = client.get(f"/api/revisions/{rid}").json()
                assert record["knowledge_status"] == expected
                expect(review.locator(".record-card")).to_contain_text(
                    "已审核" if expected == "APPROVED" else "已发布"
                )
        report = {
            "revision_id": rid,
            "pdf_pages": 2,
            "ocr": "actual local RapidOCR",
            "browser_errors": errors,
            "console_errors": console_errors,
            "duplicate_upload": True,
            "zoom_pan_rotate": True,
            "page_navigation": True,
            "ocr_boxes": True,
            "download_integrity": True,
            "viewports": [1440, 820, 390],
        }
        if record["knowledge_status"] == "PUBLISHED":
            login("agent")
            assert page.locator('[data-tab="import"]').is_hidden()
            page.locator('[data-tab="graph"]').click()
            page.locator('#graph-form [name="service"]').fill("扫描登记")
            page.locator('#graph-form [name="region"]').fill("测试甲区")
            with page.expect_response(lambda r: "/api/graph?" in r.url) as graph_response:
                page.locator("#graph-form button").click()
            graph = graph_response.value.json()
            assert graph["nodes"] and graph["edges"]
            assert {n["recordId"] for n in graph["nodes"]} == {rid}
            page.locator(".graph-node").first.wait_for()
            page.screenshot(path=str(output / "graph-desktop.png"), full_page=True)
            page.locator('[data-tab="query"]').click()
            page.locator(".chat-scope summary").click()
            page.locator('#query-form [name="service"]').fill("扫描登记")
            page.locator('#query-form [name="region"]').fill("测试甲区")
            page.locator('#query-form [name="question"]').fill(
                "扫描登记需要什么申请材料和申请条件？有哪些排除情形？"
            )
            with page.expect_response(lambda r: r.url.endswith("/api/qa"), timeout=180000) as response:
                page.locator('#query-form button[type="submit"]').click()
            answer = response.value.json()
            assert response.value.status == 200 and answer["generation"]["used"], answer
            assert "测试申请表" in answer["answer"] and "失信" in answer["answer"]
            assert {c["page"] for c in answer["citations"]} <= {1, 2}
            page.locator(".citation-button").first.wait_for()
            page.screenshot(path=str(output / "qa-desktop.png"), full_page=True)
            page.locator(".citation-button").first.click()
            page.locator("dialog svg image").wait_for()
            assert page.locator("dialog .evidence-box").count() >= 1
            page.screenshot(path=str(output / "citation-original.png"))
            report.update(
                qa=answer, graph_nodes=len(graph["nodes"]), graph_edges=len(graph["edges"]), published=True
            )
        assert not errors and not console_errors and not failed_responses, (
            errors,
            console_errors,
            failed_responses,
        )
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": "PASSED",
                    "published": record["knowledge_status"] == "PUBLISHED",
                    "revision_id": rid,
                },
                ensure_ascii=False,
            )
        )
        browser.close()


if __name__ == "__main__":
    main()
