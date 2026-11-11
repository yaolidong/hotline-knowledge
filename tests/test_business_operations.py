"""业务空间、维护诊断和反馈权限回归；图/模型替身不代表真实联调。"""

import json

from fastapi.testclient import TestClient

from conftest import guide, headers, import_ready, publish


def populate(client, app):
    test = import_ready(client, app, title="示例登记测试资料")
    publish(client, test)
    app.state.workflow.extractor.material = "正式分区夹具申请表"
    response = client.post(
        "/api/import/text",
        headers=headers(),
        json={
            "title": "示例登记正式分区夹具",
            "region": "测试甲区",
            "synthetic": False,
            "text": guide("正式分区夹具申请表"),
        },
    )
    production = response.json()["revision_id"]
    app.state.workflow.process(app.state.store.claim())
    publish(client, production)
    return production, test


def test_spaces_filter_graph_qa_sources_and_service_workbench(application):
    with TestClient(application) as client:
        production, test = populate(client, application)
        for space, expected, forbidden in [("production", production, test), ("test", test, production)]:
            query = client.post(
                "/api/query",
                headers=headers("agent"),
                json={"question": "示例登记需要什么材料", "space": space},
            ).json()
            assert query["space"] == space
            assert {x["revision_id"] for x in query["citations"]} == {expected}
            graph = client.get("/api/graph", headers=headers("agent"), params={"space": space}).json()
            assert {n["recordId"] for n in graph["nodes"]} == {expected}
            services = client.get("/api/services", headers=headers("agent"), params={"space": space}).json()
            assert {s["revision_id"] for g in services["items"] for s in g["sources"]} == {expected}
            for suffix in ["", "/text", "/info"]:
                assert (
                    client.get(
                        f"/api/source/{forbidden}{suffix}", headers=headers("agent"), params={"space": space}
                    ).status_code
                    == 403
                )
            chunk = application.state.store.get_revision(forbidden)["chunks"][0]
            assert (
                client.get(
                    f"/api/evidence/{forbidden}/{chunk['id']}/locate",
                    headers=headers("agent"),
                    params={"space": space, "start": 0, "end": 2},
                ).status_code
                == 403
            )
            assert (
                client.get(
                    f"/api/source/{expected}/text", headers=headers("agent"), params={"space": space}
                ).status_code
                == 200
            )
        assert client.get("/api/services?space=all", headers=headers()).status_code == 422
        assert (
            client.post(
                "/api/query", headers=headers(), json={"question": "查询资料", "space": "all"}
            ).status_code
            == 422
        )


def test_default_production_space_and_vectors_never_receive_test_text(application):
    from hotline.models import Question

    seen = []

    class Ranking:
        enabled = True

        def rank(self, question, texts):
            seen.extend(texts)
            return [(i, 1) for i in range(len(texts))]

    application.state.qa.semantic = Ranking()
    application.state.qa.graph_query.default_space = "production"
    with TestClient(application) as client:
        production, _ = populate(client, application)
        request = Question(question="示例登记需要什么材料")
        result = application.state.qa.graph_query.ask(request)
        sources, _ = application.state.qa._retrieve(request, result)
        assert result["space"] == "production"
        assert {s["revision_id"] for s in sources} == {production}
        assert seen and not any("申请材料：测试申请表" in text for text in seen)


def test_diagnostics_uses_actual_retrieval_without_generation_and_requires_role(application):
    class NoModel:
        def complete(self, messages):
            raise AssertionError("诊断不得生成政策答案")

    application.state.qa.model = NoModel()
    with TestClient(application) as client:
        production, _ = populate(client, application)
        request = {"question": "示例登记需要什么材料", "space": "production"}
        assert (
            client.post("/api/retrieval/diagnose", headers=headers("agent"), json=request).status_code == 403
        )
        response = client.post("/api/retrieval/diagnose", headers=headers("maintainer"), json=request)
        assert response.status_code == 200, response.text
        data = response.json()
        assert not data["generation_used"] and data["retrieval"]["candidate_count"] > 0
        assert {s["revision_id"] for s in data["selected_sources"]} == {production}
        assert any(c["selected"] and c["graph_required"] for c in data["retrieval"]["chunks"])
        assert any("OTHER_SPACE" in item["reasons"] for item in data["exclusions"])


def test_feedback_trace_privacy_ownership_concurrency_and_dedup(application):
    with TestClient(application) as client:
        production, _ = populate(client, application)
        question = "示例登记需要什么材料，来电标识不要记录"
        response = client.post(
            "/api/query", headers=headers("agent"), json={"question": question, "space": "production"}
        ).json()
        trace_id = response["trace_id"]
        with application.state.store.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM audit WHERE action='QUERY_TRACE' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert question not in row[0] and "不要记录" not in row[0]
            assert json.loads(row[0])["sources"] == [production]
        body = {"trace_id": trace_id, "category": "missing_requirement", "note": "需核对例外"}
        assert client.post("/api/feedback", headers=headers("maintainer"), json=body).status_code == 403
        first = client.post("/api/feedback", headers=headers("agent"), json=body).json()
        again = client.post("/api/feedback", headers=headers("agent"), json=body).json()
        assert first["id"] == again["id"] and again["duplicate"]
        assert client.get("/api/feedback", headers=headers("agent")).status_code == 403
        assert client.get("/api/feedback?space=test", headers=headers()).json()["total"] == 0
        item = client.get("/api/feedback?space=production", headers=headers()).json()["items"][0]
        review = {
            "expected_event_id": item["review"]["event_id"],
            "status": "RESOLVED",
            "note": "已补充回归问题",
        }
        assert (
            client.post(
                f"/api/feedback/{first['id']}/review", headers=headers("agent"), json=review
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/feedback/{first['id']}/review", headers=headers("maintainer"), json=review
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/feedback/{first['id']}/review", headers=headers("maintainer"), json=review
            ).status_code
            == 409
        )


def test_service_list_rechecks_publication(application, monkeypatch):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        original = application.state.graph.service_candidates

        def changed(ids):
            result = original(ids)
            with application.state.store.connect() as db:
                db.execute("UPDATE revisions SET knowledge_status='RETIRED' WHERE id=?", (rid,))
            return result

        monkeypatch.setattr(application.state.graph, "service_candidates", changed)
        assert client.get("/api/services", headers=headers("agent")).status_code == 409


def test_operations_summary_is_scoped_bounded_and_not_quality_metrics(application):
    with TestClient(application) as client:
        populate(client, application)
        with application.state.store.connect() as db:
            for index in range(20):
                application.state.store.audit(
                    db,
                    None,
                    "agent",
                    "QUERY_TRACE",
                    snapshot={
                        "space": "test",
                        "mode": "qa",
                        "status": "NO_EVIDENCE",
                        "latency_ms": (index + 1) * 10,
                    },
                )
            application.state.store.audit(
                db,
                None,
                "agent",
                "QUERY_TRACE",
                snapshot={"space": "production", "mode": "qa", "status": "ANSWERED", "latency_ms": 50000},
            )
        assert client.get("/api/operations/summary", headers=headers("agent")).status_code == 403
        data = client.get("/api/operations/summary?space=test", headers=headers()).json()
        assert data["traces_sampled"] == 20 and not data["truncated"]
        assert data["performance"][1]["p50_ms"] == 100 and data["performance"][1]["p95_ms"] == 190
        assert data["performance"][0]["p50_ms"] is None
        assert "准确率" in data["message"] and data["revision_states"]
        assert client.get("/api/operations/summary?days=99", headers=headers()).status_code == 422
