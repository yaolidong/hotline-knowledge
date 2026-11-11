from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish, guide
from test_qa import EvidenceModel


def decision(client, app, left, right, action="confirm", event_id=0):
    a, b = [app.state.store.get_revision(rid) for rid in (left, right)]
    return {
        "source_revision": left,
        "source_entity": "s",
        "target_revision": right,
        "target_entity": "s",
        "source_version": a["version"],
        "target_version": b["version"],
        "expected_event_id": event_id,
        "action": action,
        "note": "合成测试：核对事项名称和地区；不合并要求",
    }


def test_candidates_exclude_other_regions_and_require_reviewer(application):
    with TestClient(application) as client:
        a = import_ready(client, application, title="来源甲")
        b = import_ready(client, application, title="来源乙")
        c = import_ready(client, application, title="其他地区", region="测试乙区")
        path = f"/api/revisions/{a}/identity-candidates?entity_id=s"
        assert client.get(path, headers=headers("agent")).status_code == 403
        result = client.get(path, headers=headers()).json()
        assert [x["revision_id"] for x in result["items"]] == [b]
        body = decision(client, application, a, b)
        assert (
            client.post("/api/identity/decisions", headers=headers("maintainer"), json=body).status_code
            == 403
        )
        result = client.post("/api/identity/decisions", headers=headers("reviewer"), json=body)
        assert result.status_code == 200
        event = result.json()["associations"][0]
        assert event["active"] and not event["both_published"]
        assert client.post("/api/identity/decisions", headers=headers(), json=body).status_code == 409
        body = decision(client, application, a, c)
        assert client.post("/api/identity/decisions", headers=headers(), json=body).status_code == 409
        body = decision(client, application, a, b, "revoke", event["event_id"])
        response = client.post("/api/identity/decisions", headers=headers(), json=body)
        assert response.status_code == 200 and not response.json()["associations"][0]["active"]
        assert not application.state.graph.graphs, "身份审核不能绕过图发布"


def test_identity_invalidates_after_source_correction(application):
    with TestClient(application) as client:
        a = import_ready(client, application, title="来源甲")
        b = import_ready(client, application, title="来源乙")
        body = decision(client, application, a, b)
        assert client.post("/api/identity/decisions", headers=headers(), json=body).status_code == 200
        record = application.state.store.get_revision(b)
        value = record["extraction"]
        value["entities"][0]["display_name"] = "示例"
        assert (
            client.put(
                f"/api/revisions/{b}/extraction",
                headers=headers(),
                json={"expected_version": record["version"], "extraction": value, "note": "更正合成实体"},
            ).status_code
            == 200
        )
        result = client.get(f"/api/revisions/{a}/identity-candidates?entity_id=s", headers=headers()).json()
        assert not result["associations"][0]["active"]


def test_identity_rejects_conflicting_codes(application):
    with TestClient(application) as client:
        a = import_ready(client, application, title="来源甲")
        b = import_ready(client, application, title="来源乙")
        for rid, code in ((a, "示例"), (b, "登记")):
            record = application.state.store.get_revision(rid)
            value = record["extraction"]
            value["entities"][0]["attributes"]["service_code"] = code
            assert (
                client.put(
                    f"/api/revisions/{rid}/extraction",
                    headers=headers(),
                    json={"expected_version": record["version"], "extraction": value, "note": "合成编码冲突"},
                ).status_code
                == 200
            )
        assert not client.get(
            f"/api/revisions/{a}/identity-candidates?entity_id=s", headers=headers()
        ).json()["items"]
        assert (
            client.post(
                "/api/identity/decisions", headers=headers(), json=decision(client, application, a, b)
            ).status_code
            == 409
        )


def named_source(client, app, name):
    rid = import_ready(client, app, text=guide() + "\n补充事项名称：" + name, title=name)
    record = app.state.store.get_revision(rid)
    value = record["extraction"]
    entity = value["entities"][0]
    chunk = next(c for c in record["chunks"] if name in c["text"])
    entity["name"] = name
    entity["evidence"] = [
        {
            "chunk_id": chunk["id"],
            "quote": name,
            "start": chunk["text"].index(name),
            "end": chunk["text"].index(name) + len(name),
        }
    ]
    client.put(
        f"/api/revisions/{rid}/extraction",
        headers=headers(),
        json={"expected_version": record["version"], "extraction": value, "note": "离线夹具核对名称"},
    ).raise_for_status()
    publish(client, rid)
    return rid


def test_reviewed_identity_expands_one_hop_and_revocation_invalidates_qa(application):
    with TestClient(application) as client:
        a = import_ready(client, application)
        publish(client, a)
        b = named_source(client, application, "合成事项乙")
        c = named_source(client, application, "合成事项丙")
        body = {"question": "示例登记需要什么材料", "region": "测试甲区"}
        initial = client.post("/api/query", headers=headers("agent"), json=body).json()
        assert {s["record_id"] for s in initial["services"]} == {a}
        response = client.post(
            "/api/identity/decisions", headers=headers("reviewer"), json=decision(client, application, a, b)
        )
        response.raise_for_status()
        event_id = response.json()["associations"][0]["event_id"]
        client.post(
            "/api/identity/decisions", headers=headers(), json=decision(client, application, b, c)
        ).raise_for_status()
        expanded = client.post("/api/query", headers=headers("agent"), json=body).json()
        assert {s["record_id"] for s in expanded["services"]} == {a, b}
        assert expanded["retrieval"]["identity_links"][0]["event_id"] == event_id
        assert c not in {e["revision_id"] for e in expanded["citations"]}

        def revoke():
            client.post(
                "/api/identity/decisions",
                headers=headers(),
                json=decision(client, application, a, b, "revoke", event_id),
            ).raise_for_status()

        application.state.qa.model = EvidenceModel(callback=revoke)
        assert client.post("/api/qa", headers=headers("agent"), json=body).status_code == 409
        after = client.post("/api/query", headers=headers("agent"), json=body).json()
        assert {s["record_id"] for s in after["services"]} == {a}
        assert not after["retrieval"].get("identity_links")
