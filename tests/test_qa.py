"""离线模型替身验证证据边界，不作为真实 DeepSeek 推理验收。"""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish


class EvidenceModel:
    def __init__(self, transform=lambda value: value, callback=lambda: None):
        self.calls = []
        self.transform, self.callback = transform, callback

    def complete(self, messages):
        payload = json.loads(messages[1]["content"])
        self.calls.append(payload)
        source = next(s for s in payload["evidence"] if "测试申请表" in s["quote"])
        value = {
            "answerable": True,
            "statements": [
                {
                    "text": "原文要求提供测试申请表。",
                    "citations": [{"id": source["id"], "quote": "申请材料：测试申请表。"}],
                }
            ],
            "missing_information": [],
        }
        self.callback()
        return json.dumps(self.transform(value), ensure_ascii=False)


def ask(client, **kwargs):
    return client.post(
        "/api/qa", headers=headers("agent"), json={"question": "示例登记需要什么材料", **kwargs}
    )


def test_qa_published_evidence_and_source_access(application):
    model = EvidenceModel()
    application.state.qa.model = model
    with TestClient(application) as client:
        rid = import_ready(client, application)
        assert ask(client).json()["status"] == "NO_EVIDENCE"
        assert not model.calls
        assert client.get(f"/api/source/{rid}/text", headers=headers("agent")).status_code == 403
        publish(client, rid)
        result = ask(client).json()
        assert result["generation"]["used"] and result["status"] == "ANSWERED"
        assert result["statements"][0]["citation_ids"] == ["1"]
        citation = result["citations"][0]
        source = client.get(f"/api/evidence/{rid}/{citation['chunk_id']}", headers=headers("agent")).json()
        assert source["chunk"]["text"][citation["start"] : citation["end"]] == citation["quote"]
        assert client.get(f"/api/source/{rid}/text", headers=headers("agent")).status_code == 200
        assert model.calls[0]["graph_facts"]
        assert any("条件" in e["quote"] for e in model.calls[0]["evidence"])
        with application.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM audit WHERE action='QA_MODEL'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "mode", ["fake_id", "fake_quote", "number", "url", "broken_json", "unchecked_information"]
)
def test_unverifiable_generation_is_rejected(application, mode):
    def mutate(value):
        statement = value["statements"][0]
        if mode == "fake_id":
            statement["citations"][0]["id"] = "forged"
        if mode == "fake_quote":
            statement["citations"][0]["quote"] = "原文不存在的材料"
        if mode == "number":
            statement["text"] = "需要提供999份测试申请表。"
        if mode == "url":
            statement["text"] = "请访问https://fake.test办理。"
        if mode == "broken_json":
            return {"answer": "未验证的回答"}
        if mode == "unchecked_information":
            value["missing_information"] = ["这是2022年发布的政策，所有人均可获批"]
        return value

    application.state.qa.model = EvidenceModel(mutate)
    with TestClient(application) as client:
        publish(client, import_ready(client, application))
        result = ask(client).json()
        assert not result["generation"]["used"]
        assert result["generation"]["status"] == "FAILED"
        assert result["statements"] == []
        assert "999" not in result["answer"] and "fake.test" not in result["answer"]


def test_no_cross_region_or_validity_generation(application):
    model = EvidenceModel()
    application.state.qa.model = model
    with TestClient(application) as client:
        publish(client, import_ready(client, application))
        assert ask(client, region="不存在的地区").json()["status"] == "NO_EVIDENCE"
        result = client.post(
            "/api/qa", headers=headers("agent"), json={"question": "示例登记政策现在有效吗"}
        ).json()
        assert result["status"] == "VALIDITY_UNCONFIRMED"
        assert not model.calls


def test_retirement_during_generation_invalidates_answer(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)

        def retire():
            with application.state.store.connect() as db:
                db.execute("UPDATE revisions SET knowledge_status='RETIRED' WHERE id=?", (rid,))

        application.state.qa.model = EvidenceModel(callback=retire)
        assert ask(client).status_code == 409


def test_source_fallback_and_model_abstention(application):
    model = EvidenceModel(
        lambda value: {"answerable": False, "statements": [], "missing_information": ["收费依据"]}
    )
    application.state.qa.model = model
    with TestClient(application) as client:
        publish(client, import_ready(client, application))
        result = client.post(
            "/api/qa", headers=headers("agent"), json={"question": "示例登记收费多少钱"}
        ).json()
        assert model.calls and result["generation"]["used"]
        assert result["status"] == "NO_EVIDENCE" and not result["statements"]
        assert not result["citations"]


def test_qa_requires_authentication(application):
    with TestClient(application) as client:
        assert client.post("/api/qa", json={"question": "测试问题"}).status_code == 401


def test_generation_retry_rechecks_evidence(application):
    def first_invalid(value):
        if len(model.calls) == 1:
            value["statements"][0]["citations"][0]["quote"] = "编造的引文"
        return value

    model = EvidenceModel(first_invalid)
    application.state.qa.model = model
    with TestClient(application) as client:
        publish(client, import_ready(client, application))
        result = ask(client).json()
        assert len(model.calls) == 2 and result["generation"]["attempts"] == 2
        assert result["generation"]["used"] and "编造" not in json.dumps(result, ensure_ascii=False)


def test_numbers_are_not_matched_as_substrings():
    from hotline.qa import QAService

    with pytest.raises(ValueError, match="数字"):
        QAService._validate(
            json.dumps(
                {
                    "answerable": True,
                    "statements": [{"text": "补贴999元", "citations": [{"id": "E1", "quote": "补贴1999元"}]}],
                    "missing_information": [],
                }
            ),
            {"E1": {"quote": "补贴1999元", "absolute_start": 0}},
        )
