"""真实浏览器/DeepSeek/OpenSPG 回归；只发布本次生成的合成资料。"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest")
    admin = next(k for k, u in settings.users.items() if u.role == "admin")
    agent = next(k for k, u in settings.users.items() if u.role == "agent")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output = Path("test-results/review-publication") / run_id
    output.mkdir(parents=True, exist_ok=False)
    errors, real_review_requests = [], []
    real_id = "a71c35e4cf454e4681edaa235d960d35"
    with (
        httpx.Client(
            base_url="http://127.0.0.1:8080",
            trust_env=False,
            timeout=60,
            headers={"Authorization": "Bearer " + admin},
        ) as client,
        sync_playwright() as pw,
    ):
        before = client.get(f"/api/revisions/{real_id}").json()
        assert before["knowledge_status"] == "DRAFT"
        browser = pw.chromium.launch(
            headless=True, executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def block_real_review(route):
            # 防护真实政策：即使客户端回归失败，也不能提交未经业务审核的确认。
            real_review_requests.append(route.request.method)
            route.abort()

        page.route(f"**/api/revisions/{real_id}/review", block_real_review)
        page.goto("http://127.0.0.1:8080")
        page.locator("#token").fill(admin)
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        page.evaluate("async rid => {await tab('review'); await loadRevision(rid, 'review');}", real_id)
        page.get_by_label("审核说明", exact=True).fill("界面回归，不提交真实政策审核")
        page.get_by_role("button", name="审核通过", exact=True).click()
        page.wait_for_function(
            "document.activeElement?.matches('select[data-quality-issue][aria-invalid=true]')"
        )
        for select in page.locator("select[data-quality-issue]").all():
            select.select_option("verified")
        page.get_by_role("button", name="审核通过", exact=True).click()
        page.wait_for_function(
            "document.activeElement?.matches('input[data-issue-reason][aria-invalid=true]')"
        )
        assert "处置理由" in page.locator("#notification").inner_text()
        assert not real_review_requests
        page.screenshot(path=str(output / "required-reason.png"), full_page=True)
        after = client.get(f"/api/revisions/{real_id}").json()
        assert (after["version"], after["knowledge_status"]) == (
            before["version"],
            before["knowledge_status"],
        )

        response = client.post(
            "/api/import/text",
            json={
                "title": f"审核发布回归（合成）{run_id}",
                "synthetic": True,
                "region": "测试甲区",
                "text": "合成测试资料，不是真实政策，不得用于实际办理。\n第一条 事项名称：审核发布回归事项。办理部门：测试受理局。\n第二条 办理步骤：提交申请。\n第三条 申请材料：合成申请表。",
            },
        )
        response.raise_for_status()
        rid = response.json()["revision_id"]
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            record = client.get(f"/api/revisions/{rid}").json()
            if record["task_status"] in ("READY", "FAILED", "PARTIAL"):
                break
            time.sleep(2)
        assert record["task_status"] == "READY", record.get("error")
        assert record["metadata"]["synthetic"]
        assert all(i["code"] == "STEP_ORDER" for i in record["quality"]["issues"]), record["quality"]
        print(
            json.dumps({"synthetic_revision": rid, "entities": len(record["extraction"]["entities"])}),
            flush=True,
        )
        page.evaluate(
            "async rid => {await switchKnowledgeSpace('test'); await tab('review'); await loadRevision(rid, 'review');}",
            rid,
        )
        for select in page.locator("select[data-quality-issue]").all():
            select.select_option("verified")
        for reason in page.locator("input[data-issue-reason]").all():
            reason.fill("合成原文仅给出单一步骤，未规定序号；保留未知，不推断顺序。")
        page.get_by_label("审核说明", exact=True).fill("合成回归验证，已核对原文中的事项、部门、材料与步骤。")
        page.get_by_role("button", name="审核通过", exact=True).click()
        page.get_by_role("button", name="发布到图谱", exact=True).wait_for()
        approved = client.get(f"/api/revisions/{rid}").json()
        assert approved["knowledge_status"] == "APPROVED"
        assert (
            client.get(
                f"/api/graph?revision_id={rid}&space=test", headers={"Authorization": "Bearer " + agent}
            ).status_code
            == 403
        )
        page.get_by_role("button", name="发布到图谱", exact=True).click()
        page.wait_for_function(
            "rid => active === 'graph' && graphSnapshot?.nodes.length > 0 && graphSnapshot.nodes.every(n => n.recordId === rid)",
            arg=rid,
            timeout=90000,
        )
        published = client.get(f"/api/revisions/{rid}").json()
        assert published["knowledge_status"] == "PUBLISHED"
        response = client.get(
            f"/api/graph?revision_id={rid}&space=test", headers={"Authorization": "Bearer " + agent}
        )
        response.raise_for_status()
        graph = response.json()
        assert graph["nodes"] and graph["edges"] and graph["source"] == "openspg-neo4j"
        assert {n["recordId"] for n in graph["nodes"]} == {rid}
        # 再次进入图谱页面必须重新请求，不能沿用发布前的旧画面。
        page.get_by_role("button", name="数据导入", exact=True).click()
        with page.expect_response(lambda r: "/api/graph?" in r.url and r.status == 200):
            page.get_by_role("button", name="知识图谱", exact=True).click()
        page.screenshot(path=str(output / "published-graph.png"), full_page=True)
        assert not errors, errors
        report = {
            "real_policy_unchanged": real_id,
            "blocked_real_review_requests": real_review_requests,
            "synthetic_revision": rid,
            "status": published["knowledge_status"],
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "page_errors": errors,
            "graph_source": graph["source"],
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
