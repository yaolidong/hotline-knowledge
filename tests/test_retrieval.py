"""已发布合成标注的召回与范围隔离回归，不能作为真实政策质量指标。"""

import pytest
from fastapi.testclient import TestClient

from conftest import fixture_extraction, guide, headers, import_ready, publish
from hotline.retrieval import candidate_score, question_regions, region_matches
from hotline.schema import DomainSchema

QUESTION = "辽宁省食品餐饮许可证申请，申请人需要满足哪些条件"
REGION = "辽宁省阜新市太平区"


class NoGeneration:
    def complete(self, messages):
        raise AssertionError("待确认范围、无证据时不得调用模型")


def food_guide(client, application, *, name="食品经营许可证", region=REGION, published=True):
    class FoodExtractor:
        def extract(self, chunks, metadata, revision_id, progress):
            result = fixture_extraction(chunks)
            result.entities[0].name = name
            return DomainSchema().validate(result, chunks)

    application.state.workflow.extractor = FoodExtractor()
    rid = import_ready(
        client,
        application,
        text=guide()
        .replace("示例登记办事指南", "合成回归办事指南")
        .replace("事项名称：示例登记。", f"事项名称：{name}（合成示例登记）。"),
        region=region,
        title=f"{name}（餐饮）服务指南（合成回归）",
    )
    if published:
        publish(client, rid)
    return rid


@pytest.mark.parametrize("endpoint", ["query", "qa"])
def test_user_question_finds_published_guide_and_requires_local_scope_confirmation(application, endpoint):
    application.state.qa.model = NoGeneration()
    with TestClient(application) as client:
        rid = food_guide(client, application)
        response = client.post(f"/api/{endpoint}", headers=headers("agent"), json={"question": QUESTION})
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "NEEDS_CLARIFICATION"
        assert result["retrieval"]["method"] == "CANDIDATES"
        assert result["scope"]["region"] == "辽宁省"
        assert result["services"][0]["record_id"] == rid
        assert result["services"][0]["name"] == "食品经营许可证"
        assert result["services"][0]["region"] == REGION
        assert "餐饮" in result["services"][0]["title"]
        assert "不能直接作为辽宁省的统一规定" in result["answer"]
        assert not result["facts"] and not result["citations"]
        confirmed = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": QUESTION, "region": REGION, "service": "食品经营许可证"},
        ).json()
        assert confirmed["status"] == "ANSWERED"
        assert all(s["region"] == REGION for s in confirmed["services"])
        assert all(f["relation"] == "REQUIRES_CONDITION" for f in confirmed["facts"])
        assert {c["revision_id"] for c in confirmed["citations"]} == {rid}


def test_exact_local_region_in_question_works_and_wrong_province_does_not_fall_back(application):
    application.state.qa.model = NoGeneration()
    with TestClient(application) as client:
        food_guide(client, application)
        exact = client.post(
            "/api/query", headers=headers("agent"), json={"question": REGION + "食品经营许可证需要什么条件"}
        ).json()
        assert exact["status"] == "ANSWERED"
        assert exact["scope"]["region"] == REGION
        for request in [
            {"question": "广东省食品经营许可证申请条件"},
            {"question": "食品经营许可证申请条件", "region": "广东省"},
        ]:
            result = client.post("/api/qa", headers=headers("agent"), json=request).json()
            assert result["status"] == "NO_EVIDENCE"
            assert result["retrieval"]["method"] == "NO_REGION_MATCH"
            assert not result["facts"] and not result["citations"] and not result["generation"]["used"]


def test_draft_and_retired_guides_never_enter_candidates(application):
    with TestClient(application) as client:
        rid = food_guide(client, application, published=False)
        for stage in ("DRAFT", "PUBLISHED", "RETIRED"):
            if stage == "PUBLISHED":
                publish(client, rid)
            elif stage == "RETIRED":
                record = application.state.store.get_revision(rid)
                assert (
                    client.post(
                        f"/api/revisions/{rid}/review",
                        headers=headers("reviewer"),
                        json={
                            "action": "retire",
                            "expected_version": record["version"],
                            "note": "测试停用候选",
                        },
                    ).status_code
                    == 200
                )
            result = client.post("/api/query", headers=headers("agent"), json={"question": QUESTION}).json()
            assert bool(result["services"]) == (stage == "PUBLISHED")


def test_similar_names_and_multiple_regions_do_not_auto_merge_or_answer(application):
    with TestClient(application) as client:
        food_guide(client, application)
        food_guide(client, application, name="食品生产许可证")
        result = client.post("/api/query", headers=headers("agent"), json={"question": QUESTION}).json()
        assert result["status"] == "NEEDS_CLARIFICATION"
        assert {s["name"] for s in result["services"]} == {"食品经营许可证", "食品生产许可证"}
        assert not result["facts"]
        result = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "辽宁省和广东省食品经营许可证条件有哪些区别"},
        ).json()
        assert result["retrieval"]["method"] == "MULTIPLE_REGIONS" and not result["facts"]


def test_candidate_overflow_is_explicit_and_old_region_cannot_override_new_question(application):
    with TestClient(application) as client:
        food_guide(client, application)
        result = client.post(
            "/api/query", headers=headers("agent"), json={"question": QUESTION, "region": "测试甲区"}
        ).json()
        assert result["status"] == "NEEDS_CLARIFICATION" and result["retrieval"]["method"] == "SCOPE_CONFLICT"
        assert result["services"][0]["region"] == REGION
        application.state.graph.service_candidates = lambda records: ([], True)
        result = client.post("/api/query", headers=headers("agent"), json={"question": QUESTION}).json()
        assert result["status"] == "NEEDS_CLARIFICATION" and result["retrieval"]["method"] == "LIMIT"
        assert not result["facts"]


@pytest.mark.parametrize(
    "question,regions",
    [
        (QUESTION, ["辽宁省"]),
        ("请问广东省食品经营许可证有什么条件", ["广东省"]),
        (REGION + "申请条件", [REGION]),
        ("这个地区申请条件有哪些", []),
        ("食品经营许可证申请条件", []),
    ],
)
def test_region_hints_are_separate_from_service_keywords(question, regions):
    assert question_regions(question, [REGION]) == regions
    assert region_matches(REGION, "辽宁省")
    assert region_matches(REGION, "阜新市")
    assert not region_matches(REGION, "广东省")


def test_generic_license_words_do_not_recall_unrelated_services():
    node = {"name": "食品经营许可证", "aliases": []}
    assert candidate_score(node, "需要什么条件，怎么申请许可证", "食品经营许可证指南") == 0
    assert candidate_score(node, QUESTION, "食品经营许可证（餐饮）指南") > 0


def test_old_selected_service_cannot_override_new_question(application):
    application.state.qa.model = NoGeneration()
    with TestClient(application) as client:
        food_guide(client, application)
        other = food_guide(client, application, name="测试营业登记")
        result = client.post(
            "/api/qa",
            headers=headers("agent"),
            json={"question": "测试营业登记需要哪些材料", "region": REGION, "service": "食品经营许可证"},
        ).json()
        assert result["status"] == "NEEDS_CLARIFICATION"
        assert result["retrieval"]["method"] == "SCOPE_CONFLICT"
        assert {s["record_id"] for s in result["services"]} == {other}
        assert not result["facts"] and not result["generation"]["used"]
