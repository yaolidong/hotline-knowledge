"""使用明确 HTTP 夹具验证聊天交互与隔离边界，不代替真实模型验收。"""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

from hotline.schema import DomainSchema


def answer(question):
    return {
        "answer": question,
        "status": "ANSWERED",
        "statements": [{"text": question, "citation_ids": ["1"]}],
        "citations": [
            {
                "id": "1",
                "title": "合成资料",
                "quote": "测试申请表",
                "revision_id": "r",
                "chunk_id": "c",
                "synthetic": True,
            }
        ],
        "services": [{"name": "测试事项", "region": "测试甲区", "record_id": "r"}],
        "facts": [],
        "warnings": ["合成测试资料，不能作为真实政策使用。"],
        "missing_information": [],
        "generation": {"used": True, "model": "FIXTURE"},
        "latency_ms": 10,
    }


@pytest.fixture
def chat_page():
    executable = os.getenv(
        "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    )
    if not Path(executable).exists():
        pytest.skip("未发现本地浏览器")
    root = Path(__file__).resolve().parents[1] / "hotline" / "static"
    requests, errors = [], []

    def route(request):
        path = urlparse(request.request.url).path
        if path == "/" or path.startswith("/static/"):
            filename = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
            mime = {
                ".html": "text/html",
                ".css": "text/css",
                ".js": "application/javascript",
                ".svg": "image/svg+xml",
            }[Path(filename).suffix]
            request.fulfill(status=200, content_type=mime, body=(root / filename).read_bytes())
            return
        if path == "/api/me":
            result = {"name": "夹具客服", "role": "agent"}
        elif path == "/api/schema":
            result = DomainSchema().data
        elif path in ("/api/qa", "/api/query"):
            payload = request.request.post_data_json
            requests.append(payload)
            result = answer(payload["question"])
            if payload["question"] == "餐饮许可申请条件" and not payload.get("service"):
                result.update(
                    status="NEEDS_CLARIFICATION",
                    answer="找到相关指南，请确认具体地区后查询。",
                    statements=[],
                    citations=[],
                    generation={"used": False},
                )
                result["services"][0]["title"] = "合成餐饮指南"
        else:
            request.fulfill(status=404, body="{}")
            return
        request.fulfill(
            status=200, content_type="application/json", body=json.dumps(result, ensure_ascii=False)
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://hotline.test/**", route)
        page.goto("http://hotline.test")
        page.locator("#token").fill("fixture-only-token")
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        yield page, requests
        assert not errors
        browser.close()


def send(page, question):
    page.locator("#chat-question").fill(question)
    page.locator("#chat-question").press("Enter")
    page.wait_for_function(
        "() => !document.querySelector('#chat-send').disabled && !!document.querySelector('.qa-statement')"
    )


def test_chat_followup_scope_history_and_safe_text(chat_page):
    page, requests = chat_page
    send(page, "测试事项需要什么材料？")
    send(page, "那可以线上办理吗？")
    assert len(requests) == 2
    assert requests[1] == {
        "question": "那可以线上办理吗？",
        "region": "测试甲区",
        "service": "测试事项",
        "as_of": "",
        "space": "production",
    }
    assert page.locator(".chat-user").count() == 2
    assert page.locator(".qa-statement").count() == 2
    page.locator(".chat-scope summary").click()
    page.locator('#query-form [name="region"]').fill("测试乙区")
    send(page, '<img src=x onerror="window.pwned=1">')
    assert requests[-1]["region"] == "测试乙区"
    assert page.locator(".chat-assistant img").count() == 0
    assert page.evaluate("window.pwned === undefined")
    page.set_viewport_size({"width": 375, "height": 812})
    page.locator(".chat-answer-actions").last.get_by_role("button", name="查看依据（1）").click()
    assert page.locator("#chat-evidence").is_visible()
    page.locator("#chat-evidence-close").click()
    assert not page.locator("#chat-evidence").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.locator("#chat-new").click()
    assert page.locator(".chat-user").count() == 0
    assert page.locator('#query-form [name="region"]').input_value() == ""
    assert page.locator('#query-form [name="service"]').input_value() == ""
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


def test_candidate_click_requeries_original_question_with_confirmed_scope(chat_page):
    page, requests = chat_page
    send(page, "餐饮许可申请条件")
    assert "合成餐饮指南" in page.locator("#chat-source-list").inner_text()
    assert "待确认" in page.locator("#chat-source-list").inner_text()
    with page.expect_response("**/api/qa"):
        page.locator(".scope-choice").click()
    page.wait_for_function("() => !document.querySelector('#chat-send').disabled")
    assert requests[-1] == {
        "question": "餐饮许可申请条件",
        "region": "测试甲区",
        "service": "测试事项",
        "as_of": "",
        "space": "production",
    }
    assert page.locator(".chat-user").count() == 2


def test_stop_new_chat_and_stale_result_isolation(chat_page):
    page, requests = chat_page
    # 模拟即使 AbortSignal 触发仍晚到的响应，验证新会话绝不接纳旧结果。
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, options) => url === '/api/qa' && JSON.parse(options.body).question === '慢请求'
        ? new Promise(resolve => { window.delayedAnswer = data => resolve(new Response(JSON.stringify(data))); })
        : original(url, options);
    }""")
    page.locator("#chat-question").fill("慢请求")
    page.locator("#chat-send").click()
    assert page.locator("#chat-stop").is_visible()
    page.locator("#chat-new").click()
    send(page, "新对话的问题")
    page.evaluate("data => window.delayedAnswer(data)", answer("旧对话不应出现"))
    page.wait_for_timeout(50)
    assert page.locator(".chat-user").count() == 1
    assert "旧对话不应出现" not in page.locator("#answer").inner_text()
    page.locator("#chat-question").fill("慢请求")
    page.locator("#chat-send").click()
    page.locator("#chat-stop").click()
    page.evaluate("data => window.delayedAnswer(data)", answer("取消请求不应出现"))
    page.wait_for_function("() => !document.querySelector('#chat-send').disabled")
    assert "取消请求不应出现" not in page.locator("#answer").inner_text()
    assert "已停止等待" in page.locator("#answer").inner_text()
    assert len(requests) == 1
