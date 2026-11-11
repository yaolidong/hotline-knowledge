"""真实本地服务验收：已发布合成 OCR 指南的连续查询与真实政务图谱交互。"""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    output = Path("test-results/chat-graph")
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    token = next(key for key, user in settings.users.items() if user.role == "agent")
    report = {"mode": "REAL_HTTP_NEO4J_DEEPSEEK", "answers": [], "viewports": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv(
                "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
            ),
            headless=True,
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8080")
        page.locator("#token").fill(token)
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        page.screenshot(path=str(output / "chat-empty-desktop.png"), full_page=True)
        page.locator(".chat-scope summary").click()
        page.locator('#query-form [name="region"]').fill("测试甲区")
        questions = ["扫描登记需要什么材料？", "那可以通过什么渠道办理？"]
        for index, question in enumerate(questions, start=1):
            page.locator("#chat-question").fill(question)
            with page.expect_response("**/api/qa", timeout=120000) as response:
                page.locator("#chat-question").press("Enter")
            answer = response.value.json()
            report["answers"].append(answer)
            page.wait_for_function("() => !document.querySelector('#chat-send').disabled")
            assert answer["generation"]["used"], answer
            assert answer["status"] == "ANSWERED", answer
            assert answer["citations"]
            assert page.locator(".chat-user").count() == index
            assert page.locator(".chat-assistant").count() == index
            assert page.locator('#query-form [name="service"]').input_value() == "扫描登记"
        assert "测试申请表" in report["answers"][0]["answer"]
        assert "测试线上平台" in report["answers"][1]["answer"]
        for label, width, height in [("desktop", 1440, 1000), ("tablet", 768, 1024), ("mobile", 375, 812)]:
            page.set_viewport_size({"width": width, "height": height})
            page.screenshot(path=str(output / f"chat-{label}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            report["viewports"].append({"name": label, "overflow": False})
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator(".chat-assistant").last.locator(".citation-button").first.click()
        page.locator("dialog .source-viewer").wait_for()
        page.locator("dialog .source-viewer svg image").wait_for()
        page.screenshot(path=str(output / "chat-citation.png"), full_page=True)
        page.locator("dialog").get_by_role("button", name="关闭", exact=True).click()
        # 更换地区后独立检索，不能继续复述上一轮的材料或渠道。
        page.locator(".chat-scope summary").click()
        page.locator('#query-form [name="region"]').fill("没有已发布资料的测试地区")
        page.locator("#chat-question").fill("那需要什么材料？")
        with page.expect_response("**/api/qa", timeout=120000) as response:
            page.locator("#chat-send").click()
        unavailable = response.value.json()
        assert unavailable["status"] in ("NO_EVIDENCE", "NEEDS_CLARIFICATION")
        assert not unavailable["citations"] and not unavailable["generation"]["used"]
        report["region_switch"] = unavailable
        page.wait_for_function("() => !document.querySelector('#chat-send').disabled")
        assert page.locator(".chat-user").count() == 3
        page.locator("#chat-new").click()
        assert page.locator(".chat-user").count() == 0
        assert page.locator('#query-form [name="service"]').input_value() == ""

        page.locator('[data-tab="graph"]').click()
        page.locator('#graph-form [name="region"]').fill("辽宁省阜新市太平区")
        page.locator('#graph-form [name="service"]').fill("食品经营许可证")
        with page.expect_response("**/api/graph?**") as response:
            page.locator("#graph-form button").click()
        graph = response.value.json()
        page.locator(".graph-node").first.wait_for()
        business_nodes = [n for n in graph["nodes"] if n["entityType"] not in ("SourceDocument", "Chunk")]
        assert page.locator(".graph-node").count() == len(business_nodes)
        page.locator("#graph-sources").check()
        assert page.locator(".graph-node").count() == len(graph["nodes"])
        page.locator("#graph-sources").uncheck()
        node = page.locator('.graph-node[aria-label="食品经营许可证"]').first
        circle = node.locator("circle")
        before = circle.get_attribute("cx")
        page.locator("#graph-layout-mode").select_option("radial")
        assert circle.get_attribute("cx") != before
        page.locator("#graph-layout-mode").select_option("force")
        node.click()
        assert page.locator(".graph-node.is-selected").count() == 1
        assert page.locator(".graph-relationship.is-selected").count() > 0
        page.screenshot(path=str(output / "graph-selected-desktop.png"), full_page=True)
        page.locator("#graph-clear-selection").click()
        page.locator(".graph-legend-chip").first.click()
        assert page.locator(".graph-node.is-muted").count() > 0
        page.locator("#graph-clear-selection").click()
        for label, width, height in [("desktop", 1440, 1000), ("tablet", 768, 1024), ("mobile", 375, 812)]:
            page.set_viewport_size({"width": width, "height": height})
            page.screenshot(path=str(output / f"graph-{label}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        report["graph"] = {
            "loaded_nodes": len(graph["nodes"]),
            "loaded_edges": len(graph["edges"]),
            "business_nodes": len(business_nodes),
        }
        assert not errors, errors
        report["browser_errors"] = errors
        browser.close()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "result": "PASS",
                "real_model_answers": len(report["answers"]),
                "region_switch_abstained": True,
                "graph": report["graph"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
