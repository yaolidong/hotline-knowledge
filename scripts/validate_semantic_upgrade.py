"""真实 DeepSeek / OpenSPG / Neo4j 回归；真实指南仅新建草稿，合成资料在测试项目发布。"""

import hashlib
import json
import time
import sys
from pathlib import Path

import httpx

from hotline.graph import GraphStore
from hotline.settings import Settings

PUBLIC_GUIDE = "70e2524c3b4f4abdbeb589939ad52593"
TEXT = """合成测试资料，不是真实政策，不得用于实际办理。事项名称：测试材料核验申请。
申请主体：企业法人及自然人。办理部门：测试核验服 务局。
申请人须同时满足年满十八周岁且在测试甲区居住，并且不属于失信情形。
办理材料要求提交测试申请表，以及营业执照或者其他主体资格证明文件复印件。
办理顺序：第一步，提交申请；第二步，窗口核验；第三步，告知结果。
线上办理渠道：测试核验平台。收费标准：不收费。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def operators(rule):
    if not rule or "operator" not in rule:
        return set()
    return {rule["operator"]} | set().union(*(operators(c) for c in rule["conditions"]))


def main():
    settings = Settings()
    if not settings.namespace.startswith("HotlineTest"):
        raise SystemExit("只允许在已有 HotlineTest 测试项目执行。")
    token = next(k for k, user in settings.users.items() if user.role == "admin")
    out = Path("test-results/semantic-upgrade")
    out.mkdir(parents=True, exist_ok=True)
    report = {"mode": "REAL_DEEPSEEK_OPENSPG_NEO4J_LOCAL_EMBEDDING", "checks": {}}
    graph = GraphStore(settings)
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=180,
    ) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.json().get('detail')}")
            return response.json()

        def ready(rid):
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                record = request("GET", f"/api/revisions/{rid}")
                if record["task_status"] == "READY":
                    return record
                if record["task_status"] == "FAILED":
                    raise RuntimeError(record["error"])
                time.sleep(2)
            raise RuntimeError("抽取等待超时；任务保留，可继续查看进度")

        original = request("GET", f"/api/revisions/{PUBLIC_GUIDE}")
        original_graph = graph.browse([PUBLIC_GUIDE], limit=150)
        # 复用上次本脚本创建且成功的草稿，避免反复调用模型。
        prior_file = out / "progress.json"
        prior = json.loads(prior_file.read_text(encoding="utf-8")) if prior_file.exists() else {}
        if not prior.get("public_draft") or "--refresh-public" in sys.argv:
            prior["public_draft"] = request("POST", f"/api/revisions/{PUBLIC_GUIDE}/reprocess")["revision_id"]
            prior_file.write_text(json.dumps(prior), encoding="utf-8")
        draft = ready(prior["public_draft"])
        report["public_draft"] = draft
        assert draft["knowledge_status"] == "DRAFT" and draft["extraction"]["schema_version"] == 2
        report["checks"]["public_new_draft"] = True
        print("真实指南已完成新版本抽取，保持待审核。", flush=True)

        imported = request(
            "POST",
            "/api/import/text",
            json={"text": TEXT, "title": "语义升级合成回归 v2", "region": "测试甲区", "synthetic": True},
        )
        sample = ready(imported["revision_id"])
        rid = sample["id"]
        report["synthetic"] = sample
        (out / "extractions.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entities = sample["extraction"]["entities"]
        assert any(e["type"] == "ServiceItem" and e["name"] == "测试材料核验申请" for e in entities)
        assert any(
            e["type"] == "GovernmentDepartment" and e["canonical_name"] == "测试核验服务局" for e in entities
        )
        assert any(e["type"] == "RequiredMaterial" and "OR" in operators(e["rule"]) for e in entities)
        assert any(
            e["type"] == "EligibilityCondition" and {"AND", "NOT"} <= operators(e["rule"]) for e in entities
        )
        assert not any(
            e["type"] == "EligibilityCondition" and e["name"] == "企业法人及自然人" for e in entities
        )
        steps = [e for e in entities if e["type"] == "ProcedureStep"]
        assert len(steps) == 3 and all(e["attributes"].get("order") for e in steps)
        report["checks"]["synthetic_semantics"] = True
        for action, expected_state in (("approve", "DRAFT"), ("publish", "APPROVED")):
            if sample["knowledge_status"] != expected_state:
                continue
            sample = request(
                "POST",
                f"/api/revisions/{rid}/review",
                json={
                    "action": action,
                    "expected_version": sample["version"],
                    "acknowledged_issues": [x["id"] for x in report["synthetic"]["quality"]["issues"]],
                    "note": "合成回归：预定义语义断言通过，仅测试项目；不代表人工政策审核。",
                },
            )
        stored = graph.browse([rid], limit=150)
        assert stored["nodes"] and stored["edges"]
        assert any(
            n["name"] == "测试核验服务局" and n["metadata"].get("raw_name") == "测试核验服 务局"
            for n in stored["nodes"]
        )
        report["graph"] = stored
        result = request(
            "POST",
            "/api/qa",
            json={"question": "测试材料核验申请需要哪些材料？", "region": "测试甲区", "space": "test"},
        )
        report["qa"] = result
        assert result["status"] == "ANSWERED" and result["generation"]["used"]
        assert result["retrieval"]["original_text"] == "GRAPH_KEYWORD_VECTOR_RRF"
        assert result["citations"] and all(c.get("locator") for c in result["citations"])
        report["checks"]["real_graph_hybrid_qa"] = True
        after = request("GET", f"/api/revisions/{PUBLIC_GUIDE}")
        assert digest(original["extraction"]) == digest(after["extraction"])
        assert digest(original_graph) == digest(graph.browse([PUBLIC_GUIDE], limit=150))
        report["checks"]["published_public_guide_unchanged"] = True
    graph.close()
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "PASS",
                "checks": report["checks"],
                "public_draft": draft["id"],
                "synthetic_revision": rid,
                "facts": len(result["facts"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
