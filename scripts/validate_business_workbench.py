"""真实 DeepSeek/OpenSPG/Neo4j 与浏览器验收；只新增明确标记的合成回归资料。"""

import json
import os
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    settings = Settings()
    if not settings.namespace.startswith("HotlineTest"):
        raise SystemExit("只允许隔离的 HotlineTest 项目")
    tokens = {u.role: token for token, u in settings.users.items()}
    out = Path("test-results/business-workbench")
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    report = {"mode": "REAL_HTTP_BROWSER_DEEPSEEK_OPENSPG_NEO4J", "checks": {}, "browser_errors": []}
    checks = report["checks"]
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        trust_env=False,
        timeout=180,
        headers={"Authorization": "Bearer " + tokens["admin"]},
    ) as client:

        def get(path, **kwargs):
            response = client.get(path, **kwargs)
            response.raise_for_status()
            return response.json()

        def post(path, body):
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        if not state.get("revision_id"):
            text = (
                Path("examples/synthetic_guide.txt")
                .read_text(encoding="utf-8")
                .replace("示例登记", "测试事项工作台登记")
            )
            imported = post(
                "/api/import/text",
                {"text": text, "title": "业务工作台合成验收", "region": "测试甲区", "synthetic": True},
            )
            state["revision_id"] = imported["revision_id"]
            state_path.write_text(json.dumps(state), encoding="utf-8")
        rid = state["revision_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            record = get(f"/api/revisions/{rid}")
            if record["task_status"] == "READY":
                break
            if record["task_status"] == "FAILED":
                raise RuntimeError(record["error"])
            time.sleep(2)
        assert record["task_status"] == "READY"
        assert record["metadata"]["synthetic"]
        entity = next(e for e in record["extraction"]["entities"] if e["type"] == "ServiceItem")
        assert "测试事项工作台登记" in entity["name"]
        assert any(e["name"] == "测试申请表" for e in record["extraction"]["entities"])
        checks["real_extraction"] = True
        print("合成资料已完成真实模型抽取，开始浏览器验收。", flush=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.getenv(
                    "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
                ),
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1512, "height": 1080})
            page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))

            def login(role):
                page.goto("http://127.0.0.1:8080")
                page.locator("#token").fill(tokens[role])
                page.locator("#login-form button").click()
                page.locator("#workspace").wait_for(state="visible")

            login("admin")
            assert page.locator("#knowledge-space").input_value() == "production"
            page.locator('[data-tab="services"]').click()
            page.locator("#services-filter input[name=keyword]").fill("食品")
            page.locator("#services-filter button").click()
            page.locator("#services-list .service-card").first.wait_for()
            page.locator("#services-list").get_by_role("button", name="查看办理信息").first.click()
            page.locator(".service-overview h2").wait_for()
            assert "食品" in page.locator(".service-overview").inner_text()
            page.screenshot(path=str(out / "production-services.png"), full_page=True)
            checks["production_service_graph"] = True
            page.locator("#knowledge-space").select_option("test")
            page.locator("#test-space-banner").wait_for(state="visible")
            page.locator('[data-tab="review"]').click()
            page.locator("#review-filter input[name=search]").fill("业务工作台合成验收")
            page.locator("#review-filter select[name=state]").select_option("")
            page.locator("#review-filter button").click()
            page.locator("#review-list").get_by_role("button", name="对照审核").first.click()
            page.locator("#review-detail h2").wait_for()
            if record["knowledge_status"] == "DRAFT":
                page.get_by_text("表单修正实体、关系与证据", exact=True).click()
                editor = page.locator(".knowledge-editor")
                editor.get_by_label("选择编辑对象").wait_for(state="visible")
                options = editor.get_by_label("选择编辑对象").locator("option").all_text_contents()
                position = next(i for i, name in enumerate(options) if name.startswith("政务事项"))
                editor.get_by_label("选择编辑对象").select_option(str(position))
                page.get_by_label("审核说明", exact=True).fill(
                    "合成回归：核对原文后测试表单修正，不代表真实政策审核"
                )
                editor.get_by_label("原文名称", exact=True).fill("未出现在原文中的编造名称")
                with page.expect_response(
                    lambda r: r.request.method == "PUT" and "/extraction" in r.url
                ) as failed:
                    editor.get_by_role("button", name="保存表单修正并重新待审").click()
                assert failed.value.status == 400
                assert get(f"/api/revisions/{rid}")["version"] == record["version"]
                editor.get_by_label("原文名称", exact=True).fill(entity["name"])
                editor.get_by_label("短展示名（须来自证据，可空）").fill("测试事项")
                editor.get_by_role("button", name="检查修改摘要").click()
                page.screenshot(path=str(out / "structured-editor.png"), full_page=True)
                with page.expect_response(
                    lambda r: r.request.method == "PUT" and "/extraction" in r.url
                ) as saved:
                    editor.get_by_role("button", name="保存表单修正并重新待审").click()
                assert saved.value.status == 200
                editor.get_by_label("原文名称", exact=True).wait_for(state="hidden")
                corrected = get(f"/api/revisions/{rid}")
                assert (
                    corrected["version"] == record["version"] + 1 and corrected["knowledge_status"] == "DRAFT"
                )
                assert (
                    next(e for e in corrected["extraction"]["entities"] if e["id"] == entity["id"])[
                        "display_name"
                    ]
                    == "测试事项"
                )
                checks["form_edit_and_evidence_rejection"] = True
                # 仅合成标注资料：自动化测试审核，不冒充业务人员对真实政策的审核。
                page.get_by_role("button", name="审核通过", exact=True).wait_for()
                page.get_by_label("审核说明", exact=True).fill(
                    "合成回归已核对事项、部门、材料和原文绑定；仅用于测试"
                )
                for choice in page.locator("select[data-quality-issue]").all():
                    choice.select_option("verified")
                for reason in page.locator("input[data-issue-reason]").all():
                    reason.fill("合成回归：已逐项对照测试原文，非真实政策")
                with page.expect_response(
                    lambda r: r.request.method == "POST" and "/review" in r.url
                ) as approved:
                    page.get_by_role("button", name="审核通过", exact=True).click()
                assert approved.value.status == 200
            if get(f"/api/revisions/{rid}")["knowledge_status"] == "APPROVED":
                page.get_by_role("button", name="发布到图谱", exact=True).wait_for()
                page.get_by_label("审核说明", exact=True).fill("发布合成回归资料到测试空间")
                with page.expect_response(
                    lambda r: r.request.method == "POST" and "/review" in r.url, timeout=120000
                ) as published:
                    page.get_by_role("button", name="发布到图谱", exact=True).click()
                assert published.value.status == 200
            assert get(f"/api/revisions/{rid}")["knowledge_status"] == "PUBLISHED"
            checks["real_graph_publish"] = True
            page.locator('[data-tab="diagnostics"]').click()
            page.locator("#diagnostic-form textarea[name=question]").fill("测试事项工作台登记需要什么材料")
            page.locator("#diagnostic-form input[name=region]").fill("测试甲区")
            page.locator("#diagnostic-form input[name=service]").fill(entity["name"])
            page.locator("#diagnostic-form button").click()
            page.get_by_role("heading", name="来源过滤原因", exact=True).wait_for(timeout=120000)
            assert "图事实必留" in page.locator("#diagnostic-results").inner_text()
            page.screenshot(path=str(out / "retrieval-diagnostics.png"), full_page=True)
            checks["real_retrieval_diagnostics"] = True
            login("agent")
            question = "测试事项工作台登记需要什么材料"
            default = client.post(
                "/api/query",
                headers={"Authorization": "Bearer " + tokens["agent"]},
                json={"question": question},
            ).json()
            assert default["space"] == "production" and not any(
                c["revision_id"] == rid for c in default["citations"]
            )
            page.locator("#knowledge-space").select_option("test")
            page.locator("#query-mode").select_option("query")
            page.locator("#chat-question").fill(question)
            page.locator("#chat-send").click()
            page.locator("#answer").get_by_role("button", name="反馈答复", exact=True).wait_for(
                timeout=120000
            )
            assert "测试申请表" in page.locator("#answer").inner_text()
            page.locator("#answer").get_by_role("button", name="反馈答复", exact=True).click()
            page.locator(".answer-feedback select").select_option("helpful")
            page.locator(".answer-feedback textarea").first.fill("合成自动回归：事项材料与证据已核对")
            with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/api/feedback")
            ) as feedback:
                page.locator(".answer-feedback").get_by_role("button", name="提交反馈").click()
            assert feedback.value.status == 201
            report["feedback_id"] = feedback.value.json()["id"]
            page.screenshot(path=str(out / "agent-feedback.png"), full_page=True)
            checks["agent_space_and_feedback"] = True
            login("admin")
            page.locator("#knowledge-space").select_option("test")
            page.locator('[data-tab="feedback"]').click()
            card = page.locator(".feedback-card").filter(has_text="合成自动回归：事项材料与证据已核对").first
            card.get_by_label("反馈处理说明").fill("回归验证通过，保留测试记录")
            with page.expect_response(
                lambda r: r.request.method == "POST" and "/api/feedback/" in r.url
            ) as resolved:
                card.get_by_role("button", name="已处理", exact=True).click()
            assert resolved.value.status == 200
            checks["feedback_review"] = True
            browser.close()
        print("真实浏览器检查通过，执行 DeepSeek 证据问答。", flush=True)
        qa = post(
            "/api/qa",
            {
                "question": "测试事项工作台登记需要什么材料",
                "service": entity["name"],
                "region": "测试甲区",
                "space": "test",
            },
        )
        assert qa["generation"]["used"] and qa["citations"]
        assert {c["revision_id"] for c in qa["citations"]} == {rid}
        checks["deepseek_qa"] = True
        report.update(status="PASS", revision_id=rid, qa=qa)
        assert not report["browser_errors"], report["browser_errors"]
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "PASS", "checks": checks}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
