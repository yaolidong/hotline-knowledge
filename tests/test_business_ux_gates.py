"""阶段4验收：诊断仅维护可见；反馈与话术依赖已有接口。"""

from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish


def test_agent_cannot_access_retrieval_diagnose(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        publish(client, rid)
        denied = client.post(
            "/api/retrieval/diagnose",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "region": "测试甲区"},
        )
        assert denied.status_code == 403


def test_maintainer_can_diagnose_and_agent_gets_compliant_empty_hint(application):
    with TestClient(application) as client:
        ok = client.post(
            "/api/retrieval/diagnose",
            headers=headers("admin"),
            json={"question": "不存在事项xyz需要什么材料", "region": "测试甲区", "space": "test"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert "result" in body
        empty = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "完全不存在的事项需要什么材料", "region": "测试甲区", "space": "test"},
        ).json()
        assert empty["status"] == "NO_EVIDENCE"
        assert "合规" in empty["answer"] or "缺少" in empty["answer"] or "没有" in empty["answer"]
