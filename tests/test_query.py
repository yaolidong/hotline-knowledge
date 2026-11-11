import pytest
from fastapi.testclient import TestClient

from conftest import guide, headers, import_ready, publish
from hotline.query import classify_conflicts, organize_facts, parse_date, temporal_status


def test_answer_prioritizes_intent_and_keeps_conditions_with_distinct_sources():
    evidence = [{"chunk_id": "c", "quote": "同时满足甲、乙，并排除丙。"}]
    full = {
        "relation": "REQUIRES_CONDITION",
        "record_id": "r",
        "value": "同时满足甲、乙，并排除丙",
        "rule": {"operator": "AND", "conditions": [{"condition": "甲"}, {"condition": "乙"}]},
        "evidence": evidence,
    }
    leaf = {**full, "value": "甲", "rule": None}
    distinct = {**leaf, "evidence": [{"chunk_id": "another", "quote": "申请条件甲另有范围"}]}
    another_version = {**leaf, "record_id": "other"}
    extra = {**leaf, "value": "丁"}
    material = {
        "relation": "REQUIRES_MATERIAL",
        "record_id": "r",
        "value": "测试申请表",
        "evidence": [{"chunk_id": "m", "quote": "测试申请表"}],
    }
    result = organize_facts([leaf, full, distinct, another_version, extra, material], ["REQUIRES_MATERIAL"])
    assert result == [material, full, distinct, another_version, extra]


@pytest.mark.parametrize(
    "question,relation",
    [
        ("示例登记由哪个部门办理", "HANDLED_BY"),
        ("示例登记需要什么材料", "REQUIRES_MATERIAL"),
        ("示例登记有什么条件", "REQUIRES_CONDITION"),
        ("示例登记的办理流程", "HAS_PROCEDURE"),
        ("示例登记的政策依据", "BASED_ON"),
        ("示例登记有线上渠道吗", "HAS_CHANNEL"),
    ],
)
def test_supported_intents_with_citations(application, question, relation):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": question, "region": "测试甲区"}
        ).json()
        assert result["status"] == "ANSWERED"
        assert relation in {f["relation"] for f in result["facts"]}
        assert all(f["evidence"] for f in result["facts"])
        assert all(c["revision_id"] == rid for c in result["citations"])
        assert "合成测试" in "".join(result["warnings"])


def test_missing_information_and_validity(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        for question in ("示例登记的收费标准", "示例登记的办理时限", "完全不存在的事项需要什么材料"):
            result = client.post("/api/query", headers=headers("agent"), json={"question": question}).json()
            assert result["status"] == "NO_EVIDENCE"
            assert not result["facts"]
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": "示例登记政策现在有效吗"}
        ).json()
        assert result["status"] == "VALIDITY_UNCONFIRMED"
        assert "不能" in result["answer"]


def test_region_isolation_and_unknown_region(application):
    with TestClient(application) as client:
        for region in ("测试甲区", "测试乙区"):
            rid = import_ready(client, application, region=region)
            publish(client, rid)
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": "示例登记需要什么材料"}
        ).json()
        assert result["status"] == "NEEDS_CLARIFICATION"
        assert not result["facts"]
        result = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "region": "测试乙区"},
        ).json()
        assert result["status"] == "ANSWERED"
        assert all(s["region"] == "测试乙区" for s in result["services"])
        rid = import_ready(client, application, region="", title="未知地区")
        publish(client, rid)
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": "示例登记需要什么材料"}
        ).json()
        assert result["status"] == "NEEDS_CLARIFICATION"


def test_conflicting_versions_and_joint_provisions(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        application.state.workflow.extractor.material = "测试证明书"
        newer = import_ready(client, application, text=guide("测试证明书"), title="另一份合成测试指南")
        publish(client, newer)
        result = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料和条件", "region": "测试甲区"},
        ).json()
        assert result["status"] == "CONFLICT"
        assert {"测试申请表", "测试证明书"}.issubset({f["value"] for f in result["facts"]})
        assert len({c["revision_id"] for c in result["citations"]}) == 2
        assert len({c["chunk_id"] for c in result["citations"]}) >= 4
        assert result["conflicts"]
        assert result["conflicts"][0]["kind"] in {"mutually_exclusive", "pending_review", "complementary"}
        assert "不" in result["conflicts"][0]["message"] or "核对" in result["conflicts"][0]["message"]


def test_classify_conflicts_kinds():
    exclusive = classify_conflicts(
        {"REQUIRES_MATERIAL": {"r1": {"材料甲"}, "r2": {"材料乙"}}}
    )
    assert exclusive[0]["kind"] == "mutually_exclusive"
    complementary = classify_conflicts(
        {"REQUIRES_MATERIAL": {"r1": {"材料甲"}, "r2": {"材料甲", "材料乙"}}}
    )
    assert complementary[0]["kind"] == "complementary"


def test_expired_knowledge_is_not_used(application):
    with TestClient(application) as client:
        rid = import_ready(client, application, text=guide() + "\n失效日期：2020年1月1日")
        publish(client, rid)
        result = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "as_of": "2026-11-03"},
        ).json()
        assert result["status"] == "NO_EVIDENCE"
        assert not result["facts"]
        assert "失效" in "".join(result["warnings"])


def test_time_validation_and_no_date_implies_unknown():
    from datetime import date

    assert parse_date("2026年1月2日") == date(2026, 1, 2)
    assert parse_date("2026-02-31") is None
    assert temporal_status({"parsed_metadata": {}}, date(2026, 1, 1))["status"] == "UNCONFIRMED"
    assert (
        temporal_status({"parsed_metadata": {"effective_on": {"value": "2027-01-01"}}}, date(2026, 1, 1))[
            "status"
        ]
        == "NOT_YET_EFFECTIVE"
    )


def test_query_during_retirement_is_rejected(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        facts = application.state.graph.facts

        def retire_during_read(*args):
            result = facts(*args)
            with application.state.store.connect() as db:
                db.execute("UPDATE revisions SET knowledge_status='RETIRED' WHERE id=?", (rid,))
            return result

        application.state.graph.facts = retire_during_read
        response = client.post(
            "/api/query", headers=headers("agent"), json={"question": "示例登记需要什么材料"}
        )
        assert response.status_code == 409


def test_condition_alone_does_not_answer_missing_material(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        record = application.state.store.get_revision(rid)
        record["extraction"]["relations"] = [
            r for r in record["extraction"]["relations"] if r["type"] != "REQUIRES_MATERIAL"
        ]
        client.put(
            f"/api/revisions/{rid}/extraction",
            headers=headers(),
            json={"expected_version": 1, "extraction": record["extraction"], "note": "标注缺少材料关系"},
        )
        publish(client, rid)
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": "示例登记需要什么材料"}
        ).json()
        assert result["status"] == "NO_EVIDENCE"
        assert not result["facts"] and not result["citations"]


def test_expiry_end_date_is_inclusive():
    from datetime import date

    record = {"parsed_metadata": {"expires_on": {"label": "有效期至", "value": "2026-11-03"}}}
    assert temporal_status(record, date(2026, 11, 3))["status"] == "UNCONFIRMED"
    assert temporal_status(record, date(2026, 11, 4))["status"] == "EXPIRED"
