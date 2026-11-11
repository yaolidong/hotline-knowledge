"""真实目录抽取/图发布与版本差异验收。仅发布明确标记的合成目录。"""

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
        raise SystemExit("只能在 HotlineTest 隔离项目运行")
    tokens = {user.role: token for token, user in settings.users.items()}
    output = Path("test-results/directory-changes")
    output.mkdir(parents=True, exist_ok=True)
    checks, errors = {}, []
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        timeout=180,
        trust_env=False,
        headers={"Authorization": "Bearer " + tokens["admin"]},
    ) as client:

        def get(path, **kwargs):
            response = client.get(path, **kwargs)
            response.raise_for_status()
            return response.json()

        public_id = "70e2524c3b4f4abdbeb589939ad52593"
        draft_id = "c443bc49df3748c9ba696c35d1488d1f"
        original = get(f"/api/revisions/{public_id}")
        diff = get(f"/api/revisions/{draft_id}/changes")
        assert diff["classification"] == "REEXTRACTION" and diff["before"]["id"] == public_id
        assert diff["entity_changes"] and diff["published_candidates"]
        checks["real_policy_readonly_diff"] = True
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.getenv(
                    "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
                ),
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1512, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://127.0.0.1:8080")
            page.locator("#token").fill(tokens["admin"])
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")
            page.locator('[data-tab="operations"]').click()
            page.locator("#operations-summary table").wait_for()
            assert "不能作为压测指标或准确率" in page.locator("#operations-summary").inner_text()
            page.screenshot(path=str(output / "operations.png"), full_page=True)
            checks["real_operations_browser"] = True
            page.evaluate(
                "async rid => { await tab('review'); await loadRevision(rid, 'review'); }", draft_id
            )
            page.get_by_role("button", name="版本差异与影响", exact=True).click()
            page.locator(".change-dialog").get_by_text("关系变化", exact=False).wait_for()
            page.screenshot(path=str(output / "policy-diff.png"), full_page=True)
            page.locator(".change-dialog").get_by_role("button", name="关闭", exact=True).click()
            checks["real_diff_browser"] = True
            page.locator("#knowledge-space").select_option("test")
            page.locator('[data-tab="import"]').click()
            page.locator(".directory-import summary").click()
            page.locator("#directory-file").set_input_files("examples/synthetic_directory.csv")
            page.get_by_role("button", name="读取表头与样例", exact=True).click()
            page.locator("input[name=directory_region]").fill("测试甲区")
            page.locator("input[name=directory_source]").fill("本仓库明确标记的合成事项目录，非真实政策")
            page.locator("input[name=directory_title]").fill("结构化目录真实联调（合成）")
            page.screenshot(path=str(output / "directory-preview.png"), full_page=True)
            with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/api/import/directory")
            ) as imported:
                page.get_by_role("button", name="按映射创建抽取任务", exact=True).click()
            assert imported.value.status == 202
            rid = imported.value.json()["revision_id"]
            checks["real_csv_browser_import"] = True
            print("CSV 已通过浏览器导入，等待真实模型逐条抽取。", flush=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                record = get(f"/api/revisions/{rid}")
                if record["task_status"] in {"READY", "FAILED"}:
                    break
                time.sleep(2)
            assert record["task_status"] == "READY", record["error"]
            assert record["metadata"]["synthetic"]
            services = [e for e in record["extraction"]["entities"] if e["type"] == "ServiceItem"]
            assert {e["attributes"]["service_code"] for e in services} == {"TEST-001", "TEST-002"}
            entities = {e["id"]: e for e in record["extraction"]["entities"]}
            for service in services:
                mark = "甲" if service["attributes"]["service_code"] == "TEST-001" else "乙"
                targets = [
                    entities[r["target"]]["name"]
                    for r in record["extraction"]["relations"]
                    if r["source"] == service["id"] and r["type"] == "REQUIRES_MATERIAL"
                ]
                assert targets == [f"合成{mark}申请表"], targets
            checks["real_deepseek_rows_without_cross_binding"] = True
            for action in ("approve", "publish"):
                record = get(f"/api/revisions/{rid}")
                if record["knowledge_status"] == "PUBLISHED":
                    break
                if action == "approve" and record["knowledge_status"] == "APPROVED":
                    continue
                response = client.post(
                    f"/api/revisions/{rid}/review",
                    json={
                        "action": action,
                        "expected_version": record["version"],
                        "note": "合成目录回归：已核对事项编码、部门与材料逐行对应；非真实政策",
                        "acknowledged_issues": [i["id"] for i in record["quality"]["issues"]],
                    },
                )
                response.raise_for_status()
            graph = get("/api/graph", params={"space": "test", "keyword": "合成目录", "limit": 80})
            assert any(n.get("recordId") == rid for n in graph["nodes"])
            checks["real_openspg_graph"] = True
            evidence = services[0]["evidence"][0]
            locator = get(
                f"/api/evidence/{rid}/{evidence['chunk_id']}/locate",
                params={"start": evidence["start"], "end": evidence["end"]},
            )
            assert locator["status"] == "LOCATED" and any(
                r["header"] == "事项编码" for r in locator["regions"]
            )
            checks["real_csv_cell_evidence"] = True
            awaitable = "async rid => { await tab('results'); await loadRevision(rid, 'results'); await openSource(rid); }"
            page.evaluate(awaitable, rid)
            page.locator(".reader-text").get_by_text("事项编码：", exact=True).first.wait_for()
            page.screenshot(path=str(output / "directory-source.png"), full_page=True)
            checks["real_csv_reader"] = True
            browser.close()
        response = client.post(
            "/api/qa",
            headers={"Authorization": "Bearer " + tokens["agent"]},
            json={"question": "合成目录甲登记需要什么申请材料？", "region": "测试甲区", "space": "test"},
        )
        response.raise_for_status()
        answer = response.json()
        assert answer["generation"]["used"] and "合成甲申请表" in answer["answer"]
        assert "合成乙申请表" not in answer["answer"] and any(
            c["revision_id"] == rid for c in answer["citations"]
        )
        checks["real_deepseek_qa"] = True
        current = get(f"/api/revisions/{public_id}")
        assert current["extraction"] == original["extraction"] and current["version"] == original["version"]
        checks["published_policy_unchanged"] = True
        assert not errors, errors
        report = {
            "mode": "REAL_HTTP_BROWSER_DEEPSEEK_OPENSPG_NEO4J",
            "revision_id": rid,
            "checks": checks,
            "browser_errors": errors,
            "diff": diff,
            "answer": answer,
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"checks": checks, "browser_errors": errors}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
