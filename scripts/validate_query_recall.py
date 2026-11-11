"""原始用户问句的真实浏览器/图数据库/DeepSeek 回归；只读取已发布资料。"""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    question = "辽宁省食品餐饮许可证申请，申请人需要满足哪些条件"
    token = next(k for k, v in Settings().users.items() if v.role == "agent")
    output = Path("test-results/query-recall")
    output.mkdir(parents=True, exist_ok=True)
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
        page.locator("#chat-question").fill(question)
        with page.expect_response("**/api/qa") as response:
            page.locator("#chat-send").click()
        candidate = response.value.json()
        assert candidate["status"] == "NEEDS_CLARIFICATION"
        assert candidate["retrieval"]["method"] == "CANDIDATES"
        assert candidate["services"][0]["region"] == "辽宁省阜新市太平区"
        assert not candidate["generation"]["used"]
        page.locator(".scope-choice").wait_for()
        page.screenshot(path=str(output / "candidate.png"), full_page=True)
        with page.expect_response("**/api/qa", timeout=120000) as response:
            page.locator(".scope-choice").click()
        answer = response.value.json()
        assert answer["status"] == "ANSWERED" and answer["generation"]["used"]
        assert answer["citations"] and answer["scope"]["region"] == "辽宁省阜新市太平区"
        assert all(s["region"] == "辽宁省阜新市太平区" for s in answer["services"])
        assert all(f["relation"] == "REQUIRES_CONDITION" for f in answer["facts"])
        page.locator(".citation-button").first.wait_for()
        assert page.locator(".chat-user").count() == 2
        page.screenshot(path=str(output / "answer.png"), full_page=True)
        page.locator(".citation-button").first.click()
        page.locator("dialog mark").wait_for()
        page.screenshot(path=str(output / "source.png"), full_page=True)
        assert not errors, errors
        browser.close()
    (output / "report.json").write_text(
        json.dumps(
            {
                "mode": "REAL_HTTP_NEO4J_DEEPSEEK",
                "question": question,
                "candidate": candidate,
                "confirmed_answer": answer,
                "browser_errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "candidate_count": len(candidate["services"]),
                "condition_facts": len(answer["facts"]),
                "citations": len(answer["citations"]),
                "model": answer["generation"]["model"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
