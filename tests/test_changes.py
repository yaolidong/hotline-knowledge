"""差异比较只报告证据和提取变化，不判定政策修订。"""

from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish


def test_reextraction_diff_preserves_published_and_reports_missing_not_repealed(application):
    with TestClient(application) as client:
        original = import_ready(client, application)
        publish(client, original)
        old = application.state.store.get_revision(original)
        created = client.post(f"/api/revisions/{original}/reprocess", headers=headers()).json()["revision_id"]
        application.state.workflow.process(application.state.store.claim())
        draft = application.state.store.get_revision(created)
        extraction = draft["extraction"]
        material = next(e for e in extraction["entities"] if e["type"] == "RequiredMaterial")
        extraction["relations"] = [r for r in extraction["relations"] if r["target"] != material["id"]]
        extraction["entities"] = [e for e in extraction["entities"] if e["id"] != material["id"]]
        assert (
            client.put(
                f"/api/revisions/{created}/extraction",
                headers=headers(),
                json={"expected_version": draft["version"], "note": "模拟抽取遗漏", "extraction": extraction},
            ).status_code
            == 200
        )
        assert client.get(f"/api/revisions/{created}/changes", headers=headers("agent")).status_code == 403
        response = client.get(f"/api/revisions/{created}/changes", headers=headers("reviewer"))
        assert response.status_code == 200
        report = response.json()
        assert report["classification"] == "REEXTRACTION" and report["before"]["id"] == original
        assert not report["text_changes"]
        assert any(e["operation"] == "absent" and e["name"] == "测试申请表" for e in report["entity_changes"])
        assert report["affected_services"] and report["published_candidates"]
        assert application.state.store.get_revision(original)["extraction"] == old["extraction"]
        assert application.state.store.get_revision(original)["knowledge_status"] == "PUBLISHED"


def test_explicit_cross_file_diff_and_no_baseline(application):
    with TestClient(application) as client:
        first = import_ready(client, application)
        assert (
            client.get(f"/api/revisions/{first}/changes", headers=headers()).json()["status"] == "NO_BASELINE"
        )
        second = import_ready(
            client,
            application,
            title="另一个来源",
            text=application.state.store.get_revision(first)["text"] + "\n新增原文明示说明。",
        )
        report = client.get(
            f"/api/revisions/{second}/changes", headers=headers(), params={"before": first}
        ).json()
        assert report["classification"] == "SOURCE_CHANGE" and report["text_changes"]
        assert any("跨文件" in warning for warning in report["warnings"])
        assert (
            client.get(
                f"/api/revisions/{first}/changes", headers=headers(), params={"before": first}
            ).status_code
            == 400
        )


def test_diff_rejects_cross_space_baseline(application):
    with TestClient(application) as client:
        first = import_ready(client, application)
        response = client.post(
            "/api/import/text",
            headers=headers(),
            json={
                "title": "正式分区离线夹具",
                "text": application.state.store.get_revision(first)["text"],
                "region": "测试甲区",
                "synthetic": False,
            },
        )
        second = response.json()["revision_id"]
        application.state.workflow.process(application.state.store.claim())
        assert (
            client.get(
                f"/api/revisions/{second}/changes", headers=headers(), params={"before": first}
            ).status_code
            == 409
        )
