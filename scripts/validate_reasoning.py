"""真实图身份关联、撤销和受限推理验收；仅使用合成资料，不修改真实政策。"""

import csv
import io
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
        raise SystemExit("只允许 HotlineTest 隔离项目")
    tokens = {u.role: token for token, u in settings.users.items()}
    out = Path("test-results/reasoning")
    out.mkdir(parents=True, exist_ok=True)
    checks, errors = {}, []
    base = json.loads(Path("test-results/directory-changes/report.json").read_text(encoding="utf-8"))[
        "revision_id"
    ]
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

        original = get(f"/api/revisions/{base}")
        assert original["metadata"]["synthetic"] and original["knowledge_status"] == "PUBLISHED"
        fields = list(
            csv.DictReader(Path("examples/synthetic_directory.csv").read_text(encoding="utf-8").splitlines())
        )
        alias = fields[0].copy()
        alias["事项名称"] = "合成甲事项别称"
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(alias))
        writer.writeheader()
        writer.writerow(alias)
        data = stream.getvalue().encode("utf-8")
        metadata = {**original["metadata"], "title": "身份关联验证别称目录（合成）"}
        response = client.post(
            "/api/import/directory",
            files={"file": ("合成关联目录.csv", data)},
            data={"metadata": json.dumps(metadata)},
        )
        response.raise_for_status()
        target = response.json()["revision_id"]
        print("正在准备真实抽取的合成别称目录。", flush=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            record = get(f"/api/revisions/{target}")
            if record["task_status"] in {"READY", "FAILED"}:
                break
            time.sleep(2)
        assert record["task_status"] == "READY", record["error"]
        entity = next(e for e in record["extraction"]["entities"] if e["type"] == "ServiceItem")
        assert entity["name"] == alias["事项名称"] and entity["attributes"]["service_code"] == "TEST-001"
        assert any(e["name"] == "合成甲申请表" for e in record["extraction"]["entities"])
        for action in ("approve", "publish"):
            record = get(f"/api/revisions/{target}")
            if record["knowledge_status"] == "PUBLISHED":
                break
            if action == "approve" and record["knowledge_status"] == "APPROVED":
                continue
            post(
                f"/api/revisions/{target}/review",
                {
                    "action": action,
                    "expected_version": record["version"],
                    "note": "逐项核对合成目录，不用于现实政策",
                    "issue_resolutions": {
                        i["id"]: {
                            "status": "verified",
                            "note": "合成目录原文明示编码、部门、材料和条件已逐项核对",
                        }
                        for i in record["quality"]["issues"]
                    },
                },
            )
        source_entity = next(
            e
            for e in original["extraction"]["entities"]
            if e["type"] == "ServiceItem" and e["attributes"].get("service_code") == "TEST-001"
        )

        def decision(action, event_id):
            return {
                "source_revision": base,
                "source_entity": source_entity["id"],
                "target_revision": target,
                "target_entity": entity["id"],
                "source_version": get(f"/api/revisions/{base}")["version"],
                "target_version": get(f"/api/revisions/{target}")["version"],
                "expected_event_id": event_id,
                "action": action,
                "note": "同一合成测试实施事项不同名称，编码与地区相同；验证后撤销",
            }

        associations = get(
            f"/api/revisions/{base}/identity-candidates", params={"entity_id": source_entity["id"]}
        )["associations"]
        prior = next(
            (e for e in associations if any(ref["revision_id"] == target for ref in e["entities"])), None
        )
        event_id = prior["event_id"] if prior else 0
        if prior and prior["action"] == "confirm":
            state = post("/api/identity/decisions", decision("revoke", event_id))
            event_id = next(e["event_id"] for e in state["associations"] if e["key"] == prior["key"])
        body = {
            "question": "合成目录甲登记需要什么材料",
            "service": "合成目录甲登记",
            "region": "测试甲区",
            "service_code": "TEST-001",
            "space": "test",
        }
        assert target not in {s["record_id"] for s in post("/api/query", body)["services"]}
        response = post("/api/identity/decisions", decision("confirm", event_id))
        event = next(
            e for e in response["associations"] if any(ref["revision_id"] == target for ref in e["entities"])
        )
        try:
            expanded = post("/api/query", body)
            assert {base, target}.issubset({s["record_id"] for s in expanded["services"]}) and expanded[
                "retrieval"
            ]["identity_links"]
            answer = post("/api/qa", body)
            assert answer["generation"]["used"] and "合成甲申请表" in answer["answer"]
            checks["real_identity_graph_and_deepseek_qa"] = True
        finally:
            post("/api/identity/decisions", decision("revoke", event["event_id"]))
        after = post("/api/query", body)
        assert target not in {s["record_id"] for s in after["services"]}
        checks["real_revocation_immediate"] = True
        scope = {
            "question": "测试事项工作台登记的政策依据是什么",
            "service": "测试事项工作台登记",
            "region": "测试甲区",
            "space": "test",
        }
        paths = post("/api/reasoning/paths", scope)
        assert paths["paths"] and all(step["evidence"] for path in paths["paths"] for step in path)
        checks["real_graph_evidence_paths"] = True
        conditions = post("/api/reasoning/conditions", scope)
        assert conditions["status"] == "CHECKED" and all(
            r["evaluation"]["value"] == "UNKNOWN" for r in conditions["rules"]
        )
        stale = client.post(
            "/api/reasoning/conditions",
            json={**scope, "rule_set_hash": "old", "observations": {conditions["leaves"][0]: "TRUE"}},
        )
        assert stale.status_code == 409
        checks["real_three_value_and_version_guard"] = True
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.getenv(
                    "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
                ),
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:8080")
            page.locator("#token").fill(tokens["agent"])
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")
            page.locator("#knowledge-space").select_option("test")
            page.locator('[data-tab="services"]').click()
            page.locator("#services-filter input[name=keyword]").fill("测试事项工作台登记")
            page.locator("#services-filter button").click()
            page.locator("#services-list").get_by_role(
                "button", name="查看办理信息", exact=True
            ).first.click()
            page.get_by_role("button", name="查看政策依据路径", exact=True).click()
            page.locator("dialog summary").first.wait_for()
            page.screenshot(path=str(out / "paths.png"), full_page=True)
            page.locator("dialog").get_by_role("button", name="关闭", exact=True).click()
            page.get_by_role("button", name="辅助核对明确条件", exact=True).click()
            page.locator("dialog form select").first.wait_for()
            assert "信息不足" in page.locator("dialog").inner_text()
            page.locator("dialog form select").first.select_option("TRUE")
            with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/api/reasoning/conditions")
            ) as checked:
                page.locator("dialog").get_by_role("button", name="核对已知条件", exact=True).click()
            assert checked.value.status == 200
            page.screenshot(path=str(out / "condition-check.png"), full_page=True)
            browser.close()
        assert not errors, errors
        checks["real_browser_path_condition_workflow"] = True
        report = {
            "checks": checks,
            "browser_errors": errors,
            "source_revision": base,
            "target_revision": target,
            "paths": paths,
            "conditions": conditions,
        }
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"checks": checks, "browser_errors": errors}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
