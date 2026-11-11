"""浏览器图交互契约：响应来自明确的测试夹具，非真实服务端到端验收。"""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from hotline.schema import DomainSchema


def test_graph_interaction_with_fixture_responses():
    from playwright.sync_api import sync_playwright

    executable = os.getenv(
        "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    )
    if not Path(executable).exists():
        pytest.skip("未发现本地浏览器；可设置 HOTLINE_BROWSER_PATH")
    root = Path(__file__).resolve().parents[1] / "hotline" / "static"
    evidence = [{"chunk_id": "chunk-test", "quote": "示例事项由测试部门办理。", "start": 0, "end": 13}]
    nodes = [
        {
            "id": key,
            "name": name,
            "entityType": kind,
            "region": "测试区",
            "recordId": "r-test",
            "evidence": evidence,
        }
        for key, name, kind in [("s", "示例事项", "ServiceItem"), ("d", "测试部门", "GovernmentDepartment")]
    ]
    graph = {
        "nodes": nodes,
        "edges": [
            {
                "source": "s",
                "target": "d",
                "type": "HANDLED_BY",
                "properties": {"recordId": "r-test", "evidence": evidence},
            }
        ],
        "has_more": False,
        "source": "TEST_FIXTURE",
    }
    graph_urls = []
    errors = []

    def route(request):
        parsed = urlparse(request.request.url)
        path = parsed.path
        if path == "/" or path.startswith("/static/"):
            filename = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
            mime = {
                ".html": "text/html",
                ".css": "text/css",
                ".js": "application/javascript",
                ".svg": "image/svg+xml",
            }[Path(filename).suffix]
            request.fulfill(status=200, content_type=mime, body=(root / filename).read_bytes())
        else:
            if path == "/api/me":
                body = {"name": "夹具测试客服", "role": "agent"}
            elif path == "/api/schema":
                body = DomainSchema().data
            elif path == "/api/graph":
                graph_urls.append(request.request.url)
                body = graph
            elif path.startswith("/api/evidence"):
                body = {
                    "filename": "合成测试资料",
                    "chunk": {
                        "id": "chunk-test",
                        "page": None,
                        "section": "第一条",
                        "text": evidence[0]["quote"],
                    },
                }
            elif path == "/api/source/r-test/info":
                body = {
                    "filename": "合成测试资料.txt",
                    "page_count": 0,
                    "kind": "text",
                    "visual": False,
                    "pages": [],
                }
            elif path == "/api/source/r-test/text":
                body = {
                    "chunks": [
                        {"id": "chunk-test", "page": None, "section": "第一条", "text": evidence[0]["quote"]}
                    ]
                }
            else:
                request.fulfill(status=404, body="{}")
                return
            request.fulfill(
                status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False)
            )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://hotline.test/**", route)
        page.goto("http://hotline.test/")
        page.locator("#token").fill("fixture-only-token")
        page.locator("#login-form button").click()
        page.get_by_role("button", name="知识图谱", exact=True).click()
        # 进入页面即读取实际接口，不依赖用户额外点击检索。
        page.locator(".graph-node").first.wait_for()
        assert page.locator(".graph-node").count() == 2
        scene = page.locator("#graph-scene")
        original = scene.get_attribute("transform")
        page.get_by_role("button", name="放大 +", exact=True).click()
        assert scene.get_attribute("transform") != original
        enlarged = scene.get_attribute("transform")
        page.get_by_role("button", name="缩小 −", exact=True).click()
        assert scene.get_attribute("transform") != enlarged
        canvas = page.locator("#graph-canvas").bounding_box()
        page.mouse.move(canvas["x"] + 20, canvas["y"] + 20)
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(120)
        zoomed = scene.get_attribute("transform")
        page.mouse.down()
        page.mouse.move(canvas["x"] + 80, canvas["y"] + 65, steps=5)
        page.mouse.up()
        assert scene.get_attribute("transform") != zoomed
        page.get_by_role("button", name="适应画布", exact=True).click()
        node = page.locator('.graph-node[aria-label="示例事项"] circle')
        node.scroll_into_view_if_needed()
        before = node.get_attribute("cx")
        edge = page.locator(".graph-edge")
        edge_before = edge.get_attribute("x1")
        box = node.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 60, box["y"] + box["height"] / 2 + 35, steps=5)
        page.mouse.up()
        assert node.get_attribute("cx") != before
        assert edge.get_attribute("x1") != edge_before
        # 拖动是布局操作，不应该展开邻域或发起写库请求。
        assert len(graph_urls) == 1
        page.locator('.graph-node[aria-label="示例事项"]').click()
        page.locator("#graph-detail").get_by_role("button", name="定位原文").click()
        page.locator("dialog").wait_for()
        page.locator("dialog .reader-text").get_by_text("示例事项由测试部门办理。", exact=True).wait_for()
        assert "示例事项" in page.locator("dialog").inner_text()
        page.locator("dialog").get_by_role("button", name="关闭").click()
        page.locator("#graph-detail").get_by_role("button", name="展开邻接节点").click()
        page.wait_for_timeout(100)
        assert any("focus=s" in url for url in graph_urls)
        page.locator(".graph-edge").click(force=True)
        assert "办理部门" in page.locator("#graph-detail").inner_text()
        assert not errors
        browser.close()
