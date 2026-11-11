"""离线故障注入测试，不作为真实模型抽取质量评估。"""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import headers, RecordingGraph
from hotline.app import create_app
from hotline.extraction import Extractor, IncompleteExtraction
from hotline.models import Chunk
from hotline.schema import DomainSchema
from hotline.settings import ServiceError
from hotline.extraction_evidence import bind_evidence


class SpanModel:
    def __init__(self):
        self.calls = []
        self.fail_second = True

    def complete(self, messages):
        payload = json.loads(messages[1]["content"])
        focus = payload["focus_chunk"]
        self.calls.append(focus)
        source = next(s for s in payload["evidence_spans"] if s["chunk_id"] == focus)
        if "乙" in source["quote"] and self.fail_second:
            raise ServiceError("模拟可恢复的模型超时", 502)
        name = "乙事项" if "乙" in source["quote"] else "甲事项"
        return json.dumps(
            {
                "entities": [
                    {
                        "id": "s",
                        "type": "ServiceItem",
                        "name": name,
                        "evidence": [{"span_id": source["span_id"]}],
                    }
                ]
            }
        )


def chunks():
    return [
        Chunk(id="a", text="第一条 甲事项。", start=0, end=8),
        Chunk(id="b", text="第二条 乙事项。", start=8, end=16),
    ]


def test_partial_checkpoints_resume_without_repeating_success():
    model, saved = SpanModel(), {}
    extractor = Extractor(model, DomainSchema())
    with pytest.raises(IncompleteExtraction) as failure:
        extractor.extract(
            chunks(),
            {"document_type": "guide"},
            "r",
            checkpoint=lambda cid, outcome, result: saved.update({cid: outcome}),
        )
    assert len(failure.value.extraction.entities) == 1
    assert [saved[c]["status"] for c in ("a", "b")] == ["READY", "FAILED"]
    model.fail_second = False
    result = extractor.extract(chunks(), {"document_type": "guide"}, "r", completed=saved)
    assert len(result.entities) == 2
    assert model.calls == ["a", "b", "b"]
    assert all(e.evidence[0].quote in [c.text for c in chunks()] for e in result.entities)


def test_changed_input_invalidates_completed_chunk():
    model, saved = SpanModel(), {}
    model.fail_second = False
    extractor = Extractor(model, DomainSchema())
    extractor.extract(
        chunks(),
        {"document_type": "guide"},
        "r",
        checkpoint=lambda cid, outcome, result: saved.update({cid: outcome}),
    )
    extractor.extract(chunks(), {"document_type": "notice"}, "r", completed=saved)
    assert model.calls == ["a", "b", "a", "b"]


def test_repeated_quote_bound_by_span_instead_of_guessing():
    chunk = Chunk(id="a", text="甲事项。甲事项。", start=0, end=8)
    model = SpanModel()
    result = Extractor(model, DomainSchema()).extract([chunk], {"document_type": "guide"}, "r")
    assert result.entities[0].evidence[0].quote == chunk.text
    assert result.entities[0].evidence[0].start == 0


@pytest.mark.parametrize("bad", ["unknown_span", "changed_word", "changed_number", "ambiguous_space"])
def test_binding_does_not_accept_unverifiable_fact(bad):
    text = "甲事项，收费10元。" if bad != "ambiguous_space" else "甲 事项。甲\n事项。"
    chunk = Chunk(id="a", text=text, start=0, end=len(text))

    class BadModel:
        def complete(self, messages):
            evidence = {"chunk_id": "a", "quote": text}
            name = "甲事项"
            attrs = {}
            if bad == "unknown_span":
                evidence = {"span_id": "not-an-input-span"}
            elif bad == "changed_word":
                name = "乙事项"
            elif bad == "changed_number":
                attrs = {"fees": "100元"}
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "s",
                            "type": "ServiceItem",
                            "name": name,
                            "attributes": attrs,
                            "evidence": [evidence],
                        }
                    ]
                }
            )

    saved = {}
    with pytest.raises(ServiceError):
        Extractor(BadModel(), DomainSchema(), 0).extract(
            [chunk],
            {"document_type": "guide"},
            "r",
            checkpoint=lambda cid, outcome, result: saved.update({cid: outcome}),
        )
    assert saved["a"]["extraction"] is None
    if bad == "changed_number":
        assert saved["a"]["candidate"]["entities"][0]["attributes"]["fees"] == "100元"


def test_only_whitespace_is_restored_and_logged():
    chunk = Chunk(id="a", text="第一条 食品\n经营许可。收费：免 费。", start=0, end=22)

    class LayoutModel:
        def complete(self, messages):
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "s",
                            "type": "ServiceItem",
                            "name": "食品经营许可",
                            "attributes": {"fees": "免费"},
                            "evidence": [{"chunk_id": "a", "quote": "食品经营许可。收费：免费。"}],
                        }
                    ]
                }
            )

    result = Extractor(LayoutModel(), DomainSchema(), 0).extract([chunk], {"document_type": "guide"}, "r")
    entity = result.entities[0]
    assert entity.name == "食品\n经营许可"
    assert entity.attributes["fees"] == "免 费"
    assert chunk.text[entity.evidence[0].start : entity.evidence[0].end] == entity.evidence[0].quote
    assert {n["kind"] for n in result.normalizations} >= {"FIELD_WHITESPACE", "EVIDENCE_WHITESPACE"}


def test_adjacent_spans_support_a_wrapped_name_but_gaps_are_not_filled():
    chunk = Chunk(id="a", text="食品\n经营许可", start=0, end=7)
    base = {
        "entities": [
            {
                "id": "s",
                "type": "ServiceItem",
                "name": "食品经营许可",
                "evidence": [
                    {"chunk_id": "a", "quote": "食品\n", "start": 0, "end": 3},
                    {"chunk_id": "a", "quote": "经营许可", "start": 3, "end": 7},
                ],
            }
        ]
    }
    from hotline.models import Extraction

    result = DomainSchema().validate(Extraction.model_validate(bind_evidence(base, [chunk])), [chunk])
    assert result.entities[0].name == chunk.text
    assert len(result.entities[0].evidence) == 1
    base["entities"][0]["evidence"][0].update(quote="食品", start=0, end=2)
    # 即便缺口只是换行，也不能把未引用的范围自动加进证据。
    with pytest.raises(ServiceError):
        DomainSchema().validate(Extraction.model_validate(bind_evidence(base, [chunk])), [chunk])


def test_api_partial_blocks_publication_and_resumes_after_restart(settings):
    model = SpanModel()
    app = create_app(settings, extractor=Extractor(model, DomainSchema(), 0), graph=RecordingGraph())
    with TestClient(app) as client:
        rid = client.post(
            "/api/import/text",
            headers=headers(),
            json={"text": "第一条 甲事项。\n第二条 乙事项。", "synthetic": True},
        ).json()["revision_id"]
        app.state.workflow.process(app.state.store.claim())
        record = client.get(f"/api/revisions/{rid}", headers=headers()).json()
        assert record["task_status"] == "PARTIAL" and len(record["extraction"]["entities"]) == 1
        assert client.get("/api/revisions?state=PARTIAL", headers=headers()).json()["total"] == 1
        for action in ("approve", "publish"):
            assert (
                client.post(
                    f"/api/revisions/{rid}/review",
                    headers=headers(),
                    json={
                        "action": action,
                        "expected_version": record["version"],
                        "note": "不能发布不完整文档",
                    },
                ).status_code
                == 409
            )
        assert app.state.store.visible_ids(published_only=True) == []
        assert client.get(f"/api/source/{rid}", headers=headers("agent")).status_code == 403
        url = f"/api/revisions/{rid}/retry-extraction"
        assert client.post(url, headers=headers("agent"), json={"expected_version": 1}).status_code == 403
        assert client.post(url, headers=headers(), json={"expected_version": 99}).status_code == 409
        assert client.post(url, headers=headers(), json={"expected_version": 1}).status_code == 202
        assert client.post(url, headers=headers(), json={"expected_version": 1}).status_code == 409
    # 新应用实例使用 SQLite 保存的成功块，不依赖进程内缓存。
    model.fail_second = False
    restarted = create_app(settings, extractor=Extractor(model, DomainSchema(), 0), graph=RecordingGraph())
    with TestClient(restarted):
        restarted.state.workflow.process(restarted.state.store.claim())
        record = restarted.state.store.get_revision(rid)
        assert record["task_status"] == "READY" and record["knowledge_status"] == "DRAFT"
        assert len(record["extraction"]["entities"]) == 2
        assert len(model.calls) == 3
        assert restarted.state.store.visible_ids(published_only=True) == []


def test_duplicate_triples_keep_all_real_evidence():
    chunk = Chunk(id="c", text="甲事项由甲部门办理。甲部门负责甲事项。", start=0, end=19)

    class DuplicateModel:
        def complete(self, messages):
            ev = {"chunk_id": "c", "quote": chunk.text}
            return json.dumps(
                {
                    "entities": [
                        {"id": "s", "type": "ServiceItem", "name": "甲事项", "evidence": [ev]},
                        {"id": "d", "type": "GovernmentDepartment", "name": "甲部门", "evidence": [ev]},
                    ],
                    "relations": [
                        {
                            "source": "s",
                            "target": "d",
                            "type": "HANDLED_BY",
                            "evidence": [{"chunk_id": "c", "quote": text}],
                        }
                        for text in ("甲事项由甲部门办理。", "甲部门负责甲事项。")
                    ],
                }
            )

    result = Extractor(DuplicateModel(), DomainSchema(), 0).extract([chunk], {"document_type": "guide"}, "r")
    assert len(result.relations) == 1
    assert len(result.relations[0].evidence) == 2
    assert any(n["kind"] == "DUPLICATE_RELATION" for n in result.normalizations)
