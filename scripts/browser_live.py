"""真实浏览器全流程：粘贴合成文本、DeepSeek 抽取、审核发布、真实图浏览、客服问答及原文。"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from hotline.settings import Settings

settings = Settings()
assert settings.namespace.startswith("HotlineTest")
tokens = {user.role: key for key, user in settings.users.items()}
base = "http://127.0.0.1:8080"
output = Path("test-results/browser-live")
output.mkdir(parents=True, exist_ok=True)
text = Path("examples/synthetic_guide.txt").read_text(encoding="utf-8").replace("示例登记", "浏览器测试登记")
title = "浏览器全流程合成指南"
with (
    httpx.Client(
        base_url=base, headers={"Authorization": "Bearer " + tokens["admin"]}, trust_env=False, timeout=30
    ) as client,
    sync_playwright() as playwright,
):
    executable = os.getenv(
        "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    )
    browser = playwright.chromium.launch(
        headless=True, **({"executable_path": executable} if Path(executable).exists() else {})
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1080})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    page.locator("#token").fill(tokens["admin"])
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")
    page.locator("#knowledge-space").select_option("test")
    page.get_by_role("button", name="数据导入", exact=True).click()
    page.locator('#import-form [name="title"]').fill(title)
    page.locator('#import-form [name="region"]').fill("测试甲区")
    page.locator('#import-form [name="synthetic"]').check()
    page.locator("#import-text-mode").click()
    page.locator('#import-form [name="text"]').fill(text)
    with page.expect_response(lambda r: r.url.endswith("/api/import/text")) as imported:
        page.locator('#import-form button[type="submit"]').click()
    rid = imported.value.json()["revision_id"]
    print("真实浏览器文本导入：" + rid, flush=True)
    page.get_by_role("button", name="抽取结果", exact=True).click()
    for _ in range(180):
        record = client.get(f"/api/revisions/{rid}").json()
        if record["task_status"] in ("READY", "FAILED"):
            break
        time.sleep(1)
    assert record["task_status"] == "READY", record["error"]
    entities = {e["id"]: e for e in record["extraction"]["entities"]}
    assert {"浏览器测试登记", "测试服务局", "测试申请表"}.issubset({e["name"] for e in entities.values()})
    assert any(e["rule"] and "不属于失信情形" in e["name"] for e in entities.values())
    assert {
        "HANDLED_BY",
        "REQUIRES_MATERIAL",
        "REQUIRES_CONDITION",
        "HAS_PROCEDURE",
        "HAS_CHANNEL",
        "BASED_ON",
    }.issubset(
        {
            r["type"]
            for r in record["extraction"]["relations"]
            if entities[r["source"]]["name"] == "浏览器测试登记"
        }
    )
    page.locator("#refresh-results").click()
    page.locator("#results-list tr").filter(has_text=title).first.get_by_role("button", name="查看").click()
    page.locator("#extraction-detail summary").filter(has_text="政务事项 · 浏览器测试登记").first.click()
    page.locator("#extraction-detail").get_by_role("button", name="定位原文").first.wait_for()
    page.screenshot(path=str(output / "extraction.png"), full_page=True)
    if record["knowledge_status"] != "PUBLISHED":
        before = client.post(
            "/api/query",
            json={
                "question": "浏览器测试登记需要什么材料",
                "service": "浏览器测试登记",
                "region": "测试甲区",
                "space": "test",
            },
        ).json()
        assert not any(c["revision_id"] == rid for c in before["citations"])
        page.get_by_role("button", name="审核与维护", exact=True).click()
        page.locator("#review-list tr").filter(has_text=title).first.get_by_role(
            "button", name="对照审核"
        ).click()
        for action, status in [("审核通过", "DRAFT"), ("发布到图谱", "APPROVED")]:
            if record["knowledge_status"] != status:
                continue
            page.get_by_label("审核说明", exact=True).fill(
                "真实浏览器验收：已对照合成原文核对关键实体、关系和完整条件"
            )
            for choice in page.locator("select[data-quality-issue]").all():
                choice.select_option("verified")
            for reason in page.locator("input[data-issue-reason]").all():
                reason.fill("合成资料验收，已核对对应原文要求")
            with page.expect_response(
                lambda r: r.url.endswith(f"/api/revisions/{rid}/review"), timeout=150000
            ) as reviewed:
                page.get_by_role("button", name=action, exact=True).click()
            assert reviewed.value.status == 200, reviewed.value.text()
            record = reviewed.value.json()
            page.get_by_role(
                "button", name="发布到图谱" if status == "DRAFT" else "停用", exact=True
            ).wait_for()
    assert record["knowledge_status"] == "PUBLISHED"
    print("真实浏览器审核与图写入回读通过", flush=True)
    page.get_by_role("button", name="知识图谱", exact=True).click()
    page.get_by_label("行政区划", exact=True).fill("测试甲区")
    page.get_by_label("事项", exact=True).fill("浏览器测试登记")
    page.get_by_role("button", name="检索图谱", exact=True).click()
    page.locator('.graph-node[aria-label="浏览器测试登记"]').first.wait_for()
    assert page.locator(".graph-edge").count() > 0
    page.locator('.graph-node[aria-label="测试申请表"]').first.click()
    page.locator("#graph-detail").get_by_role("button", name="定位原文").first.click()
    page.locator("dialog mark").get_by_text("第二条 申请材料：测试申请表。", exact=True).wait_for()
    page.screenshot(path=str(output / "graph-evidence.png"), full_page=True)
    page.get_by_role("button", name="关闭", exact=True).click()
    page.locator('.graph-node[aria-label="浏览器测试登记"]').first.click()
    with page.expect_response(lambda r: "/api/graph?" in r.url):
        page.get_by_role("button", name="展开邻接节点", exact=True).click()
    page.locator('.graph-node[aria-label="测试申请表"]').first.wait_for()
    page.screenshot(path=str(output / "graph.png"), full_page=True)
    page.locator("#logout").click()
    page.locator("#token").fill(tokens["agent"])
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")
    page.locator("#knowledge-space").select_option("test")
    assert not page.get_by_role("button", name="数据导入", exact=True).is_visible()
    page.locator(".chat-scope summary").click()
    page.locator('#query-form [name="region"]').fill("测试甲区")
    page.locator('#query-form [name="service"]').fill("浏览器测试登记")
    page.locator('#query-form [name="question"]').fill("浏览器测试登记需要什么材料？")
    page.locator("#query-mode").select_option("query")
    with page.expect_response(lambda r: r.url.endswith("/api/query")) as queried:
        page.locator('#query-form button[type="submit"]').click()
    answer = queried.value.json()
    assert answer["status"] == "ANSWERED" and "测试申请表" in answer["answer"]
    assert "不属于失信情形" in answer["answer"]
    page.locator("#answer").get_by_role("button", name="定位原文").first.wait_for()
    page.screenshot(path=str(output / "query.png"), full_page=True)
    page.locator("#answer").get_by_role("button", name="定位原文").first.click()
    page.locator("dialog mark").wait_for()
    assert page.locator("dialog mark").inner_text() in record["text"]
    assert not errors, errors
    report = {
        "passed": True,
        "revision_id": rid,
        "time": datetime.now(timezone.utc).isoformat(),
        "model": settings.llm_model,
        "knowledge_status": record["knowledge_status"],
        "entities": len(entities),
        "relations": len(record["extraction"]["relations"]),
        "answer": answer,
        "page_errors": errors,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()
    print("浏览器真实全流程通过，报告与截图：" + str(output), flush=True)
