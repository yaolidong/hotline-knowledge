import concurrent.futures

from fastapi.testclient import TestClient

from conftest import guide, headers, import_ready, publish
from hotline.app import create_app
from hotline.models import ImportMetadata
from hotline.store import Store


def test_roles_upload_and_empty_query(application):
    with TestClient(application) as client:
        assert client.get("/api/revisions").status_code == 401
        assert client.get("/api/revisions", headers=headers("agent")).status_code == 403
        assert (
            client.post("/api/import/text", headers=headers("agent"), json={"text": "abc"}).status_code == 403
        )
        assert (
            client.post("/api/import/text", headers=headers("reviewer"), json={"text": "abc"}).status_code
            == 403
        )
        response = client.post("/api/query", headers=headers("agent"), json={"question": "需要什么材料"})
        assert response.json()["status"] == "NO_EVIDENCE"
        assert not response.json()["facts"]


def test_graph_can_focus_on_published_revision_without_crossing_permissions(application):
    with TestClient(application) as client:
        first = import_ready(client, application)
        second = import_ready(client, application, title="另一份合成指南")
        url = f"/api/graph?revision_id={first}"
        assert client.get(url, headers=headers("agent")).status_code == 403
        publish(client, first)
        publish(client, second)
        response = client.get(url, headers=headers("agent"))
        assert response.status_code == 200
        assert response.json()["nodes"]
        assert {n["recordId"] for n in response.json()["nodes"]} == {first}
        assert client.get(url + "&space=production", headers=headers()).status_code == 403


def test_dedup_concurrency_metadata_and_revisions(settings):
    store = Store(settings.data_dir)
    meta = ImportMetadata(region="甲区")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.ingest(b"data", "test.txt", ".txt", meta, "admin"), range(8)))
    assert len({r["document_id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1
    other = store.ingest(b"data", "test.txt", ".txt", ImportMetadata(region="乙区"), "admin")
    assert other["document_id"] != results[0]["document_id"]


def test_upload_signature_size_path_and_invalid_metadata(application):
    with TestClient(application) as client:
        assert (
            client.post(
                "/api/import/files", headers=headers(), files={"files": ("a.pdf", b"notpdf")}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/import/files", headers=headers(), files={"files": ("a.exe", b"abc")}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/import/files",
                headers=headers(),
                files={"files": ("a.txt", b"abc")},
                data={"metadata": "bad"},
            ).status_code
            == 400
        )
        response = client.post(
            "/api/import/files",
            headers=headers(),
            files=[("files", ("../../a.txt", b"abc")), ("files", ("b.txt", b"def"))],
        )
        assert response.status_code == 202
        row = application.state.store.get_revision(response.json()["items"][0]["revision_id"])
        assert row["filename"] == "a.txt"
        assert (
            client.post(
                "/api/import/files", headers={**headers(), "Content-Length": "999999999"}, content=b"x"
            ).status_code
            == 413
        )


def test_state_machine_evidence_and_retire(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        record = application.state.store.get_revision(rid)
        chunk = record["chunks"][0]["id"]
        assert client.get(f"/api/evidence/{rid}/{chunk}", headers=headers("agent")).status_code == 403
        bad = client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "publish", "expected_version": 1, "note": "绕过审核"},
        )
        assert bad.status_code == 409
        assert (
            client.post(
                f"/api/revisions/{rid}/review",
                headers=headers("maintainer"),
                json={"action": "approve", "expected_version": 1, "note": "无权限"},
            ).status_code
            == 403
        )
        publish(client, rid)
        assert client.get(f"/api/evidence/{rid}/{chunk}", headers=headers("agent")).status_code == 200
        assert client.get(f"/api/source/{rid}", headers=headers("agent")).content.decode() == guide()
        answer = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "region": "测试甲区"},
        ).json()
        assert answer["status"] == "ANSWERED"
        assert any(f["value"] == "测试申请表" for f in answer["facts"])
        assert any(f["rule"] for f in answer["facts"])
        assert all(e["quote"] in record["text"] for e in answer["citations"])
        version = application.state.store.get_revision(rid)["version"]
        retired = client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "retire", "expected_version": version, "note": "测试停用"},
        )
        assert retired.status_code == 200
        assert (
            client.post(
                "/api/query", headers=headers("agent"), json={"question": "示例登记需要什么材料"}
            ).json()["status"]
            == "NO_EVIDENCE"
        )
        assert client.get(f"/api/evidence/{rid}/{chunk}", headers=headers("agent")).status_code == 403


def test_publish_failure_keeps_approved_and_allows_retry(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "approve", "expected_version": 1, "note": "测试"},
        )
        application.state.graph.fail = True
        response = client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "publish", "expected_version": 2, "note": "测试"},
        )
        assert response.status_code == 502
        record = application.state.store.get_revision(rid)
        assert record["knowledge_status"] == "APPROVED"
        assert application.state.store.visible_ids() == []
        application.state.graph.fail = False
        response = client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "publish", "expected_version": record["version"], "note": "恢复后重试"},
        )
        assert response.status_code == 200


def test_correction_invalidates_approval_and_rejects_stale_version(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        client.post(
            f"/api/revisions/{rid}/review",
            headers=headers(),
            json={"action": "approve", "expected_version": 1, "note": "测试"},
        )
        record = application.state.store.get_revision(rid)
        body = {"expected_version": 1, "extraction": record["extraction"], "note": "重新整理"}
        assert client.put(f"/api/revisions/{rid}/extraction", headers=headers(), json=body).status_code == 409
        body["expected_version"] = record["version"]
        response = client.put(f"/api/revisions/{rid}/extraction", headers=headers(), json=body)
        assert response.json()["knowledge_status"] == "DRAFT"
        publish(client, rid)
        body["expected_version"] = application.state.store.get_revision(rid)["version"]
        assert client.put(f"/api/revisions/{rid}/extraction", headers=headers(), json=body).status_code == 409
        new = client.post(f"/api/revisions/{rid}/reprocess", headers=headers()).json()
        assert new["revision_id"] != rid
        assert application.state.store.get_revision(rid)["knowledge_status"] == "PUBLISHED"
        assert len(application.state.store.history(rid)) >= 4


def test_real_unconfigured_model_preserves_parse_and_fails(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        rid = client.post("/api/import/text", headers=headers(), json={"text": guide()}).json()["revision_id"]
        app.state.workflow.process(app.state.store.claim())
        record = app.state.store.get_revision(rid)
        assert record["task_status"] == "FAILED"
        assert "模型服务尚未配置" in record["error"]
        assert record["chunks"] and record["text"]
        assert record["extraction"] is None
        response = client.get("/api/graph", headers=headers())
        assert response.status_code == 503
        assert "nodes" not in response.json()


def test_schema_endpoint_and_xss_text_is_data(application):
    with TestClient(application) as client:
        assert len(client.get("/api/schema", headers=headers()).json()["entities"]) == 10
        response = client.get("/")
        assert "script-src 'self'" in response.headers["content-security-policy"]
        rid = client.post(
            "/api/import/text",
            headers=headers(),
            json={"text": "<script>alert(1)</script>", "title": "<img onerror=alert(1)>"},
        ).json()["revision_id"]
        data = client.get(f"/api/revisions/{rid}", headers=headers()).json()
        assert data["metadata"]["title"] == "<img onerror=alert(1)>"
        # JSON 数据不能被当作 HTML；前端统一使用 textContent。
        script = client.get("/static/app.js").text
        assert "innerHTML" not in script
