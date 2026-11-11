"""受限推理离线边界，真实图联调另行运行。"""

import pytest
from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish
from hotline.reasoning import evaluate_rule


@pytest.mark.parametrize(
    "op,values,expected",
    [
        ("AND", ["TRUE", "UNKNOWN"], "UNKNOWN"),
        ("AND", ["FALSE", "UNKNOWN"], "FALSE"),
        ("AND", ["TRUE", "TRUE"], "TRUE"),
        ("OR", ["FALSE", "UNKNOWN"], "UNKNOWN"),
        ("OR", ["TRUE", "UNKNOWN"], "TRUE"),
        ("OR", ["FALSE", "FALSE"], "FALSE"),
        ("NOT", ["UNKNOWN"], "UNKNOWN"),
        ("NOT", ["TRUE"], "FALSE"),
        ("NOT", ["FALSE"], "TRUE"),
    ],
)
def test_three_valued_logic(op, values, expected):
    tree = {"operator": op, "conditions": [{"condition": str(i)} for i in range(len(values))]}
    assert evaluate_rule(tree, {str(i): v for i, v in enumerate(values)})["value"] == expected


def test_bounded_paths_use_published_edges_and_citations(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        body = {"question": "示例登记政策依据是什么", "service": "示例登记", "region": "测试甲区"}
        assert not client.post("/api/reasoning/paths", headers=headers("agent"), json=body).json()["paths"]
        record = application.state.store.get_revision(rid)
        value = record["extraction"]
        value["relations"].append(
            {"source": "c", "target": "l", "type": "BASED_ON", "evidence": value["entities"][-1]["evidence"]}
        )
        client.put(
            f"/api/revisions/{rid}/extraction",
            headers=headers(),
            json={"expected_version": record["version"], "note": "合成依据路径标注", "extraction": value},
        ).raise_for_status()
        publish(client, rid)
        report = client.post("/api/reasoning/paths", headers=headers("agent"), json=body).json()
        assert {len(p) for p in report["paths"]} == {1, 2}
        assert all(step["evidence"] and step["record_id"] == rid for path in report["paths"] for step in path)
        assert (
            client.post(
                "/api/reasoning/paths", headers=headers(), json={**body, "plan": "arbitrary_cypher"}
            ).status_code
            == 422
        )


def test_condition_inputs_require_current_rules_and_do_not_decide_eligibility(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        body = {"question": "查询办理条件", "service": "示例登记", "region": "测试甲区"}
        initial = client.post("/api/reasoning/conditions", headers=headers("agent"), json=body).json()
        assert initial["rules"][0]["evaluation"]["value"] == "UNKNOWN"
        observed = {"年满十八周岁": "TRUE", "居住在测试甲区": "TRUE", "失信情形": "FALSE"}
        request = {**body, "observations": observed, "rule_set_hash": initial["rule_set_hash"]}
        checked = client.post("/api/reasoning/conditions", headers=headers("agent"), json=request).json()
        assert checked["rules"][0]["evaluation"]["value"] == "TRUE"
        assert "行政决定" in checked["message"] and "eligible" not in checked
        assert (
            client.post(
                "/api/reasoning/conditions", headers=headers(), json={**request, "rule_set_hash": "old"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/reasoning/conditions",
                headers=headers(),
                json={**request, "observations": {"模型编造条件": "TRUE"}},
            ).status_code
            == 400
        )
        record = application.state.store.get_revision(rid)
        client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "retire", "expected_version": record["version"], "note": "合成停用验证"},
        ).raise_for_status()
        assert (
            client.post("/api/reasoning/conditions", headers=headers(), json=request).json()["status"]
            == "UNAVAILABLE"
        )
