"""核对合成模型结果，实际审核发布到 OpenSPG，并查询真实 Neo4j 和证据。"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision_id")
    args = parser.parse_args()
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest"), "仅允许独立测试项目"
    token = next(key for key, user in settings.users.items() if user.role == "admin")
    output = Path("data/validation") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-graph")
    output.mkdir(parents=True)
    report = {"revision_id": args.revision_id, "namespace": settings.namespace, "checks": []}
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=180,
    ) as client:
        record = client.get(f"/api/revisions/{args.revision_id}").json()
        assert record["task_status"] == "READY" and record["metadata"]["synthetic"]
        entities = {e["id"]: e for e in record["extraction"]["entities"]}
        services = [e for e in entities.values() if e["type"] == "ServiceItem"]
        assert services and {e["name"] for e in services} == {"示例登记"}
        required = {
            "HANDLED_BY": "测试服务局",
            "REQUIRES_MATERIAL": "测试申请表",
            "HAS_PROCEDURE": "提交申请",
            "HAS_CHANNEL": "测试线上平台",
            "BASED_ON": "合成示例管理规定",
            "REQUIRES_CONDITION": "年满十八周岁",
        }
        for relation, value in required.items():
            assert any(
                r["type"] == relation
                and entities[r["source"]]["type"] == "ServiceItem"
                and value in entities[r["target"]]["name"]
                for r in record["extraction"]["relations"]
            ), relation
        assert any(e["rule"] and "不属于失信情形" in e["name"] for e in entities.values())
        if record["knowledge_status"] == "DRAFT":
            before = client.post(
                "/api/query", json={"question": "示例登记需要什么材料", "region": "测试甲区"}
            ).json()
            assert not any(c["revision_id"] == args.revision_id for c in before["citations"])
            report["checks"].append("draft_excluded")
        for action, state in [("approve", "DRAFT"), ("publish", "APPROVED")]:
            if record["knowledge_status"] != state:
                continue
            response = client.post(
                f"/api/revisions/{args.revision_id}/review",
                json={
                    "action": action,
                    "expected_version": record["version"],
                    "note": "软件验收：对照合成原文检查核心办理字段及证据，不是正式政策审核",
                },
            )
            (output / f"{action}.json").write_text(response.text, encoding="utf-8")
            assert response.status_code == 200, response.text
            record = response.json()
        assert record["knowledge_status"] == "PUBLISHED"
        graph = client.get("/api/graph", params={"region": "测试甲区", "limit": 150}).json()
        assert graph["source"] == "openspg-neo4j" and graph["nodes"] and graph["edges"]
        report.update(nodes=len(graph["nodes"]), edges=len(graph["edges"]), knowledge_status="PUBLISHED")
        (output / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        questions = [
            ("示例登记由哪个部门办理", "ANSWERED", "测试服务局", "测试甲区"),
            ("示例登记需要什么材料", "ANSWERED", "测试申请表", "测试甲区"),
            ("示例登记有哪些条件", "ANSWERED", "不属于失信情形", "测试甲区"),
            ("示例登记的办理流程", "ANSWERED", "窗口核验", "测试甲区"),
            ("示例登记有没有线上渠道", "ANSWERED", "测试线上平台", "测试甲区"),
            ("示例登记的政策依据", "ANSWERED", "合成示例管理规定", "测试甲区"),
            ("示例登记政策现在有效吗", "VALIDITY_UNCONFIRMED", "", "测试甲区"),
            ("示例登记收费多少", "NO_EVIDENCE", "", "测试甲区"),
            ("示例登记需要什么材料", "NO_EVIDENCE", "", "测试乙区"),
        ]
        answers = []
        for question, status, value, region in questions:
            response = client.post("/api/query", json={"question": question, "region": region})
            assert response.status_code == 200, response.text
            answer = response.json()
            answers.append({"question": question, "region": region, **answer})
            assert answer["status"] == status, (question, answer)
            assert value in answer["answer"], (question, answer)
            if status == "ANSWERED":
                assert answer["citations"]
                for citation in answer["citations"]:
                    source = client.get(f"/api/evidence/{citation['revision_id']}/{citation['chunk_id']}")
                    assert source.status_code == 200
                    assert citation["quote"] in source.json()["chunk"]["text"]
            report["checks"].append(question + " / " + region)
        (output / "answers.json").write_text(
            json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["query_latency_ms"] = [a["latency_ms"] for a in answers]
        report["passed"] = True
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({**report, "output": str(output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
