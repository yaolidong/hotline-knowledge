"""真实服务浏览器验证：缩放/平移/节点拖动、源文件下载、DeepSeek QA 与逐句原文引用。"""

import json
import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--public-guide", action="store_true", help="验证已核对并发布到测试项目的真实办事指南正文"
    )
    args = parser.parse_args()
    region = "辽宁省阜新市太平区" if args.public_guide else "测试甲区"
    service = "食品经营许可证" if args.public_guide else "示例登记"
    settings = Settings()
    token = next(key for key, user in settings.users.items() if user.role == "agent")
    output = Path(
        "test-results/browser-public-policy" if args.public_guide else "test-results/browser-enhancements"
    )
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        executable = os.getenv(
            "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        )
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200}, accept_downloads=True)
        errors, mutations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                mutations.append(request.url) if request.method in ("PUT", "PATCH", "DELETE") else None
            ),
        )
        page.goto("http://127.0.0.1:8080")
        page.locator("#token").fill(token)
        page.locator("#login-form button").click()
        page.get_by_role("button", name="知识图谱", exact=True).click()
        page.get_by_label("行政区划", exact=True).fill(region)
        page.get_by_label("事项", exact=True).fill(service)
        page.get_by_role("button", name="检索图谱", exact=True).click()
        page.locator(".graph-node").first.wait_for()
        scene = page.locator("#graph-scene")
        before = scene.get_attribute("transform")
        page.get_by_role("button", name="放大 +", exact=True).click()
        assert scene.get_attribute("transform") != before
        page.get_by_role("button", name="缩小 −", exact=True).click()
        canvas = page.locator("#graph-canvas")
        canvas.scroll_into_view_if_needed()
        bounds = canvas.bounding_box()
        page.mouse.move(bounds["x"] + 30, bounds["y"] + 30)
        before = scene.get_attribute("transform")
        page.mouse.wheel(0, -200)
        page.wait_for_timeout(150)
        assert scene.get_attribute("transform") != before
        before = scene.get_attribute("transform")
        page.mouse.down()
        page.mouse.move(bounds["x"] + 75, bounds["y"] + 70, steps=5)
        page.mouse.up()
        assert scene.get_attribute("transform") != before
        page.get_by_role("button", name="适应画布", exact=True).click()
        node = page.locator(f'.graph-node[aria-label="{service}"]').first
        circle = node.locator("circle")
        circle.scroll_into_view_if_needed()
        bounds = circle.bounding_box()
        before = circle.get_attribute("cx")
        page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            bounds["x"] + bounds["width"] / 2 + 75, bounds["y"] + bounds["height"] / 2 + 20, steps=5
        )
        page.mouse.up()
        assert circle.get_attribute("cx") != before
        node.click()
        page.locator("#graph-detail").get_by_role("button", name="定位原文", exact=True).first.click()
        page.locator("dialog mark").wait_for()
        with page.expect_download() as downloaded:
            page.locator("dialog").get_by_role("button", name="下载原文件", exact=True).click()
        downloaded.value.save_as(str(output / "source.txt"))
        assert (output / "source.txt").stat().st_size > 0
        page.locator("dialog").get_by_role("button", name="查看解析全文", exact=True).click()
        page.locator("dialog .source-fulltext").wait_for()
        assert "申请材料" in page.locator("dialog .source-fulltext").inner_text()
        page.screenshot(path=str(output / "source.png"), full_page=True)
        page.locator("dialog").get_by_role("button", name="关闭", exact=True).click()
        page.screenshot(path=str(output / "graph.png"), full_page=True)
        page.get_by_role("button", name="全屏", exact=True).click()
        page.wait_for_function("() => !!document.fullscreenElement")
        page.get_by_role("button", name="全屏", exact=True).click()
        page.wait_for_function("() => !document.fullscreenElement")
        page.get_by_role("button", name="客服查询", exact=True).click()
        page.locator(".chat-scope summary").click()
        page.locator('#query-form [name="region"]').fill(region)
        page.locator('#query-form [name="service"]').fill(service)
        page.locator('#query-form [name="question"]').fill(f"{service}需要什么材料和申请条件？")
        page.locator("#query-mode").select_option("qa")
        with page.expect_response(
            lambda response: response.url.endswith("/api/qa"), timeout=180000
        ) as queried:
            page.locator('#query-form button[type="submit"]').click()
        result = queried.value.json()
        assert result["generation"]["used"] and result["status"] == "ANSWERED", result
        if args.public_guide:
            assert "食品经营许可申请书" in result["answer"] and "营业执照" in result["answer"]
        else:
            assert "测试申请表" in result["answer"] and "失信" in result["answer"]
        page.locator(".citation-button").first.wait_for()
        page.screenshot(path=str(output / "qa.png"), full_page=True)
        page.locator(".citation-button").first.click()
        page.locator("dialog mark").wait_for()
        assert page.locator("dialog mark").inner_text()
        assert not errors and not mutations, (errors, mutations)
        (output / "report.json").write_text(
            json.dumps(
                {
                    "graph": "actual-neo4j",
                    "zoom_pan_drag": True,
                    "fullscreen": True,
                    "source_download": True,
                    "source_text": True,
                    "citation_dialog": True,
                    "qa": result,
                    "browser_errors": errors,
                    "mutations": mutations,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "PASSED",
                    "model": result["generation"]["model"],
                    "qa_latency_ms": result["latency_ms"],
                },
                ensure_ascii=False,
            )
        )
        browser.close()


if __name__ == "__main__":
    main()
