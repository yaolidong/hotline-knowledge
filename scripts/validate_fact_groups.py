"""真实 DeepSeek/浏览器/OpenSPG/Neo4j 联调；只审核本脚本的明确合成资料。"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from hotline.fact_review import fingerprint, objects
from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest")
    token = next(k for k, user in settings.users.items() if user.role == "admin")
    agent = next(k for k, user in settings.users.items() if user.role == "agent")
    stamp = args.resume.name if args.resume else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output = args.resume or Path("test-results/fact-groups") / stamp
    if not args.resume:
        output.mkdir(parents=True, exist_ok=False)
    first, second = f"合成分组甲登记{stamp}", f"合成分组乙登记{stamp}"
    source = f"合成软件测试资料，非真实政策，不得用于实际办理。\n第一条 {first}需要提交甲申请表。\n第二条 {second}需要提交乙申请表。"
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        trust_env=False,
        timeout=120,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        real_id = "a71c35e4cf454e4681edaa235d960d35"
        before = client.get(f"/api/revisions/{real_id}").json()
        response = (
            client.post(
                "/api/import/text",
                json={
                    "title": "事实组审核合成回归 " + stamp,
                    "text": source,
                    "region": "测试甲区",
                    "synthetic": True,
                },
            )
            if not args.resume
            else None
        )
        if response is not None:
            response.raise_for_status()
        rid = (
            response.json()["revision_id"]
            if response is not None
            else json.loads((output / "input.json").read_text(encoding="utf-8"))["revision_id"]
        )
        (output / "input.json").write_text(
            json.dumps({"revision_id": rid, "source": source}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"revision_id": rid, "output": str(output)}), flush=True)
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            record = client.get(f"/api/revisions/{rid}").json()
            if record["task_status"] in ("READY", "PARTIAL", "FAILED"):
                break
            time.sleep(2)
        assert record["task_status"] == "READY", record.get("error")
        entities = {e["id"]: e for e in record["extraction"]["entities"]}
        triples = {
            (entities[r["source"]]["name"], r["type"], entities[r["target"]]["name"])
            for r in record["extraction"]["relations"]
        }
        required = {
            (first, "REQUIRES_MATERIAL", "甲申请表"),
            (second, "REQUIRES_MATERIAL", "乙申请表"),
        }
        assert required.issubset(triples), triples
        assert triples - required <= {(first, "BASED_ON", "第一条"), (second, "BASED_ON", "第二条")}, triples
        assert all(
            e["type"] in ("ServiceItem", "RequiredMaterial", "PolicyDocument", "PolicyProvision")
            for e in entities.values()
        )
        review = client.get(f"/api/revisions/{rid}/fact-review").json()
        group = next(g for g in review["groups"] if first in g["title"])
        assert not group["quality"]["issues"], group["quality"]
        assert (
            client.get(
                f"/api/graph?revision_id={rid}&space=test", headers={"Authorization": "Bearer " + agent}
            ).status_code
            == 403
        )
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://127.0.0.1:8080")
            page.locator("#token").fill(token)
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")
            page.evaluate(
                "async rid => {await switchKnowledgeSpace('test'); await tab('review'); await loadRevision(rid, 'review');}",
                rid,
            )
            panel = page.locator(f'[data-group-id="{group["id"]}"]')
            panel.locator(":scope > summary").click()
            row = panel.locator(".fact-row").first
            row.locator(":scope > summary").click()
            row.get_by_label("处理依据", exact=True).fill("合成回归：先退回，再对照上述两句测试原文确认")
            row.get_by_role("button", name="退回修正", exact=True).click()
            page.get_by_text("已退回；相关事实组暂不能发布。", exact=True).wait_for()
            row.get_by_label("处理依据", exact=True).fill(
                "合成原文已逐字核对，实体及所需材料关系与人工预期一致"
            )
            row.get_by_role("button", name="确认此条", exact=True).click()
            page.get_by_text("条目处理已保存。", exact=True).wait_for()
            for checkbox in panel.locator("[data-group-check]").all():
                checkbox.check()
            panel.locator(".coverage-review > summary").click()
            for select in panel.locator("[data-coverage-chunk]").all():
                select.evaluate("node => node.closest('details').open = true")
                value = select.locator("option").nth(1).get_attribute("value")
                select.select_option(value)
                if value == "unrelated":
                    cid = select.get_attribute("data-coverage-chunk")
                    panel.get_by_label(f"排除依据 {cid}", exact=True).fill(
                        "该块为测试声明或独立乙事项；两句原文无交叉条件及例外"
                    )
            panel.get_by_label("事实组审核依据", exact=True).fill(
                "人工对照合成标准：甲登记对应甲申请表，乙登记对应乙申请表；原文无其他条件"
            )
            page.screenshot(path=str(output / "group-review.png"), full_page=True)
            panel.get_by_role("button", name="审核通过此组", exact=True).click()
            panel.get_by_role("button", name="发布此组", exact=True).wait_for()
            panel.get_by_role("button", name="发布此组", exact=True).click()
            page.wait_for_function(
                "() => active === 'graph' && graphSnapshot?.nodes.length > 0", timeout=90000
            )
            snapshot_id = page.locator('#graph-form [name="revision_id"]').input_value()
            page.screenshot(path=str(output / "published-graph.png"), full_page=True)
            assert not errors, errors
            browser.close()
        snapshot = client.get(f"/api/revisions/{snapshot_id}").json()
        assert snapshot["knowledge_status"] == "PUBLISHED"
        assert snapshot["document_id"] == record["document_id"]
        assert set(objects(snapshot)) == set(group["item_keys"])
        graph = client.get(
            f"/api/graph?revision_id={snapshot_id}&space=test", headers={"Authorization": "Bearer " + agent}
        ).json()
        assert graph["source"] == "openspg-neo4j" and graph["nodes"] and graph["edges"]
        answer = client.post(
            "/api/qa",
            headers={"Authorization": "Bearer " + agent},
            json={
                "question": first + "需要提交什么材料？",
                "service": first,
                "region": "测试甲区",
                "space": "test",
            },
        )
        answer.raise_for_status()
        answer = answer.json()
        assert answer["generation"]["used"], answer
        assert {c["revision_id"] for c in answer["citations"]} == {snapshot_id}
        assert "甲申请表" in answer["answer"]
        after = client.get(f"/api/revisions/{real_id}").json()
        assert fingerprint(before) == fingerprint(after)
        report = {
            "source_revision": rid,
            "snapshot_revision": snapshot_id,
            "groups": len(review["groups"]),
            "published_group": group["title"],
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "graph_source": graph["source"],
            "generation": answer["generation"],
            "page_errors": errors,
            "real_policy_unchanged": real_id,
            "source_status": client.get(f"/api/revisions/{rid}").json()["knowledge_status"],
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output / "qa.json").write_text(json.dumps(answer, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
