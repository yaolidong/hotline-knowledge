"""事实组审核边界；内存图为明确契约替身，真实图验证另行运行。"""

import copy

import pytest
from fastapi.testclient import TestClient

from conftest import headers
from hotline.fact_review import CHECKS, fact_groups, fingerprint
from hotline.models import Chunk, Extraction, ImportMetadata, Question
from hotline.parsing import Parsed
from hotline.schema import DomainSchema
from hotline.store import dumps


def seed(app, partial=False):
    texts = [
        "合成测试：甲登记要求同时满足年满十八周岁和居住甲区，并且不属于失信情形。",
        "合成测试：乙登记需要乙申请表。",
        "合成测试：丙登记需要丙申请表。",
    ]
    text = "\n".join(texts)
    rid = app.state.store.ingest(
        text.encode(), "合成分组.txt", ".txt", ImportMetadata(synthetic=True, region="测试甲区"), "admin"
    )["revision_id"]
    offset, chunks = 0, []
    for i, value in enumerate(texts):
        chunks.append(
            Chunk(id=f"c{i}", text=value, start=offset, end=offset + len(value), section=f"第{i + 1}条")
        )
        offset += len(value) + 1
    app.state.store.save_parse(rid, Parsed(text, chunks, {}))
    entities, relations = [], []
    for i, letter in enumerate("甲乙丙"[: 2 if partial else 3]):
        evidence = [{"chunk_id": f"c{i}", "quote": texts[i], "start": 0, "end": len(texts[i])}]
        entities.append({"id": f"s{i}", "type": "ServiceItem", "name": letter + "登记", "evidence": evidence})
        target = {
            "id": f"t{i}",
            "type": "EligibilityCondition" if i == 0 else "RequiredMaterial",
            "name": "同时满足年满十八周岁和居住甲区，并且不属于失信情形" if i == 0 else letter + "申请表",
            "evidence": evidence,
        }
        if i == 0:
            target["rule"] = {"condition": target["name"]}
        entities.append(target)
        relations.append(
            {
                "source": f"s{i}",
                "target": f"t{i}",
                "type": "REQUIRES_CONDITION" if i == 0 else "REQUIRES_MATERIAL",
                "evidence": evidence,
            }
        )
    extraction = DomainSchema().validate(
        Extraction(schema_version=2, entities=entities, relations=relations), chunks
    )
    app.state.store.finish(rid, extraction, "ANNOTATED_TEST")
    meta = {
        "_extraction": {
            c.id: {
                "status": "FAILED" if partial and i == 2 else "READY",
                "attempts": 1,
                "error": None,
                "extraction": Extraction().model_dump(),
            }
            for i, c in enumerate(chunks)
        }
    }
    with app.state.store.connect() as db:
        db.execute(
            "UPDATE revisions SET parsed_metadata=?,task_status=? WHERE id=?",
            (dumps(meta), "PARTIAL" if partial else "READY", rid),
        )
    return rid


def state(client, rid):
    response = client.get(f"/api/revisions/{rid}/fact-review", headers=headers())
    response.raise_for_status()
    return response.json()


def group_body(app, rid, group):
    record = app.state.store.get_revision(rid)
    return {
        "action": "approve",
        "expected_version": record["version"],
        "note": "合成标注核对，完整条件与例外均已保留",
        "checks": dict.fromkeys(CHECKS, True),
        "coverage": {
            c["id"]: {
                "status": "included" if c["id"] in group["chunk_ids"] else "unrelated",
                "note": "合成原文属于另一个独立事项，无交叉引用",
            }
            for c in record["chunks"]
        },
        "issue_resolutions": {
            i["id"]: {"status": "verified", "note": "合成原文核对"} for i in group["quality"]["issues"]
        },
    }


def approve(client, app, rid, group):
    response = client.post(
        f"/api/revisions/{rid}/groups/{group['id']}/review",
        headers=headers("reviewer"),
        json=group_body(app, rid, group),
    )
    assert response.status_code == 200, response.text


def publish_group(client, app, rid, group):
    return client.post(
        f"/api/revisions/{rid}/groups/{group['id']}/publish",
        headers=headers("reviewer"),
        json={"expected_version": app.state.store.get_revision(rid)["version"], "note": "发布已审核合成组"},
    )


def test_group_publication_preserves_source_and_excludes_other_facts(application):
    with TestClient(application) as client:
        rid = seed(application, partial=True)
        group = state(client, rid)["groups"][0]
        assert len(state(client, rid)["groups"]) == 2
        before = application.state.store.get_revision(rid)
        approve(client, application, rid, group)
        response = publish_group(client, application, rid, group)
        assert response.status_code == 200, response.text
        snapshot = response.json()
        assert snapshot["id"] != rid and snapshot["knowledge_status"] == "PUBLISHED"
        assert application.state.store.get_revision(rid)["knowledge_status"] == "DRAFT"
        assert application.state.store.get_revision(rid)["task_status"] == "PARTIAL"
        assert application.state.store.get_revision(rid)["extraction"] == before["extraction"]
        assert len(snapshot["extraction"]["entities"]) == 2
        assert len(snapshot["chunks"]) == 1
        assert {c["id"] for c in snapshot["chunks"]}.isdisjoint(c["id"] for c in before["chunks"])
        assert snapshot["text"] == before["text"]  # 原件阅读仍保留全文，RAG 只读取快照 chunks。
        for c in snapshot["chunks"]:
            assert snapshot["text"][c["start"] : c["end"]] == c["text"]
        assert application.state.store.visible_ids() == [snapshot["id"]]
        assert publish_group(client, application, rid, group).json()["id"] == snapshot["id"]
        assert client.get(f"/api/graph?revision_id={rid}", headers=headers("agent")).status_code == 403
        assert (
            client.get(f"/api/graph?revision_id={snapshot['id']}", headers=headers("agent")).status_code
            == 200
        )

        question = Question(
            question=group["title"] + "需要哪些条件和材料？",
            service=group["title"],
            region="测试甲区",
            space="test",
        )
        result = application.state.qa.graph_query.ask(question)
        evidence, error = application.state.qa._retrieve(question, result)
        assert not error and evidence
        assert {e["revision_id"] for e in evidence} == {snapshot["id"]}
        assert all("丙登记" not in e["quote"] for e in evidence)
        assert all(e["quote"] in snapshot["chunks"][0]["text"] for e in evidence)
        duplicate = application.state.store.ingest(
            before["text"].encode(),
            "合成分组.txt",
            ".txt",
            ImportMetadata(synthetic=True, region="测试甲区"),
            "admin",
        )
        assert duplicate["duplicate"] and duplicate["revision_id"] == rid


@pytest.mark.parametrize("error", ["checks", "coverage", "reason", "failed_source", "stale", "permission"])
def test_group_approval_requires_complete_current_review(application, error):
    with TestClient(application) as client:
        rid = seed(application)
        group = state(client, rid)["groups"][0]
        body = group_body(application, rid, group)
        role = "reviewer"
        if error == "checks":
            body["checks"]["conditions"] = False
        elif error == "coverage":
            body["coverage"].pop(next(iter(body["coverage"])))
        elif error == "reason":
            next(c for c in body["coverage"].values() if c["status"] == "unrelated")["note"] = "  "
        elif error == "stale":
            body["expected_version"] += 1
        elif error == "permission":
            role = "maintainer"
        else:
            record = application.state.store.get_revision(rid)
            record["parsed_metadata"]["_extraction"][group["chunk_ids"][0]]["status"] = "FAILED"
            with application.state.store.connect() as db:
                db.execute(
                    "UPDATE revisions SET parsed_metadata=? WHERE id=?",
                    (dumps(record["parsed_metadata"]), rid),
                )
        response = client.post(
            f"/api/revisions/{rid}/groups/{group['id']}/review", headers=headers(role), json=body
        )
        assert response.status_code in (403, 409)
        assert not application.state.graph.graphs


def test_returned_item_blocks_group_until_corrected_and_rechecked(application):
    with TestClient(application) as client:
        rid = seed(application)
        group = next(g for g in state(client, rid)["groups"] if "甲登记" in g["title"])
        url = f"/api/revisions/{rid}/facts/entity:t0/decision"
        response = client.post(
            url,
            headers=headers("reviewer"),
            json={"action": "return", "expected_version": 1, "note": "核对完整排除条件"},
        )
        assert response.status_code == 200
        assert (
            client.post(
                f"/api/revisions/{rid}/groups/{group['id']}/review",
                headers=headers(),
                json=group_body(application, rid, group),
            ).status_code
            == 409
        )
        record = application.state.store.get_revision(rid)
        target = next(e for e in record["extraction"]["entities"] if e["id"] == "t0")
        target["attributes"]["content"] = target["name"]
        corrected = client.put(
            f"/api/revisions/{rid}/extraction",
            headers=headers(),
            json={
                "expected_version": record["version"],
                "note": "保留完整条件原文",
                "extraction": record["extraction"],
            },
        )
        assert corrected.status_code == 200
        assert next(i for i in state(client, rid)["items"] if i["key"] == "entity:t0")["status"] == "PENDING"
        group = next(g for g in state(client, rid)["groups"] if "甲登记" in g["title"])
        approve(client, application, rid, group)
        record = application.state.store.get_revision(rid)
        assert (
            client.post(
                f"/api/revisions/{rid}/review",
                headers=headers(),
                json={
                    "action": "approve",
                    "expected_version": record["version"],
                    "note": "尝试旧接口绕过分组",
                },
            ).status_code
            == 409
        )
        history = application.state.store.history(rid)
        assert any(e["action"] == "FACT_RETURN" for e in history)


def test_manual_failed_block_validates_evidence_and_keeps_existing_facts(application):
    with TestClient(application) as client:
        rid = seed(application, partial=True)
        before = application.state.store.get_revision(rid)
        text = before["chunks"][2]["text"]
        part = {
            "entities": [
                {
                    "id": "s2",
                    "type": "ServiceItem",
                    "name": "丙登记",
                    "evidence": [{"chunk_id": "c2", "quote": text, "start": 0, "end": len(text)}],
                }
            ]
        }
        body = {"expected_version": 1, "note": "人工对照合成第三条补录", "extraction": part}
        url = f"/api/revisions/{rid}/chunks/c2/manual"
        assert client.put(url, headers=headers("reviewer"), json=body).status_code == 403
        bad = copy.deepcopy(body)
        bad["extraction"]["entities"][0]["name"] = "原文没有的事项"
        assert client.put(url, headers=headers(), json=bad).status_code == 400
        response = client.put(url, headers=headers(), json=body)
        assert response.status_code == 200, response.text
        record = response.json()
        assert record["task_status"] == "READY" and record["knowledge_status"] == "DRAFT"
        assert len(record["extraction"]["entities"]) == len(before["extraction"]["entities"]) + 1
        assert record["parsed_metadata"]["_extraction"]["c2"]["method"] == "manual"
        assert record["parsed_metadata"]["_manual_baseline"] == record["extraction"]
        assert not application.state.graph.graphs
        assert client.put(url, headers=headers(), json=body).status_code == 409


def test_no_facts_requires_explicit_decision(application):
    with TestClient(application) as client:
        rid = seed(application, partial=True)
        body = {"expected_version": 1, "note": "合成空块处置", "extraction": {}}
        url = f"/api/revisions/{rid}/chunks/c2/manual"
        assert client.put(url, headers=headers(), json=body).status_code == 409
        body["no_facts"] = True
        assert client.put(url, headers=headers(), json=body).status_code == 200


def test_publish_failure_retries_same_snapshot_and_copy_keeps_history(application):
    with TestClient(application) as client:
        rid = seed(application)
        group = state(client, rid)["groups"][0]
        approve(client, application, rid, group)
        application.state.graph.fail = True
        assert publish_group(client, application, rid, group).status_code == 502
        sid = state(client, rid)["groups"][0]["decision"]["publication_revision_id"]
        assert application.state.store.visible_ids() == []
        application.state.graph.fail = False
        assert publish_group(client, application, rid, group).json()["id"] == sid
        record = application.state.store.get_revision(rid)
        assert (
            client.put(
                f"/api/revisions/{rid}/extraction",
                headers=headers(),
                json={
                    "expected_version": record["version"],
                    "note": "不能覆盖",
                    "extraction": record["extraction"],
                },
            ).status_code
            == 409
        )
        response = client.post(
            f"/api/revisions/{rid}/review-copy",
            headers=headers(),
            json={"expected_version": record["version"], "note": "继续修订未发布事实"},
        )
        assert response.status_code == 201
        clone = response.json()
        assert clone["id"] != rid and clone["extraction"] == record["extraction"]
        assert clone["knowledge_status"] == "DRAFT" and not clone["parsed_metadata"]["_fact_review"]["groups"]
        assert application.state.store.get_revision(sid)["knowledge_status"] == "PUBLISHED"


def test_retry_keeps_manual_baseline_without_model_overwrite():
    from hotline.extraction import Extractor
    from test_extraction_recovery import SpanModel, chunks

    model = SpanModel()
    model.fail_second = False
    source = chunks()
    baseline = {
        "entities": [
            {
                "id": "manual",
                "type": "ServiceItem",
                "name": "甲事项",
                "attributes": {},
                "evidence": [{"chunk_id": "a", "quote": source[0].text}],
            }
        ]
    }
    saved = {
        "a": {
            "status": "READY",
            "method": "manual",
            "source_hash": fingerprint(source[0].model_dump()),
            "extraction": Extraction().model_dump(),
        }
    }
    result = Extractor(model, DomainSchema()).extract(
        source, {"document_type": "guide"}, "r", completed=saved, baseline=baseline
    )
    assert model.calls == ["b"]
    assert {e.name for e in result.entities} == {"甲事项", "乙事项"}


def test_overlapping_evidence_groups_cannot_be_published_separately(application):
    rid = seed(application)
    record = application.state.store.get_revision(rid)
    first = record["extraction"]["entities"][0]
    clone = copy.deepcopy(first)
    clone["id"] = "second-mention"
    record["extraction"]["entities"].append(clone)
    assert len(fact_groups(record)) == 3
