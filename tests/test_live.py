"""显式启用的真实全链路验收；无服务时跳过，绝不以替身替代。"""

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import guide
from hotline.app import create_app
from hotline.settings import Settings


@pytest.mark.live
def test_real_model_openspg_neo4j_end_to_end(tmp_path):
    if os.getenv("HOTLINE_RUN_LIVE") != "1":
        pytest.skip("未配置真实 OpenSPG/Neo4j/模型；设置 HOTLINE_RUN_LIVE=1 显式启用")
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest"), (
        "真实验收必须使用独立 HotlineTest 命名空间与空测试项目"
    )
    assert settings.openspg_project_id > 0 and settings.llm_model and settings.llm_base_url
    settings.data_dir = tmp_path
    settings.worker_enabled = True
    settings.default_knowledge_space = "test"
    token = "test-" + uuid.uuid4().hex
    from hotline.settings import User

    settings.users = {token: User(name="live-test-reviewer", role="admin")}
    app = create_app(settings)
    authorization = {"Authorization": "Bearer " + token}
    with TestClient(app) as client:
        response = client.post(
            "/api/import/files",
            headers=authorization,
            files={"files": ("合成测试指南.txt", guide().encode("utf-8"), "text/plain")},
            data={"metadata": '{"region":"测试甲区","synthetic":true,"document_type":"guide"}'},
        )
        assert response.status_code == 202, response.text
        rid = response.json()["items"][0]["revision_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            record = client.get(f"/api/revisions/{rid}", headers=authorization).json()
            if record["task_status"] in ("READY", "FAILED"):
                break
            time.sleep(1)
        assert record["task_status"] == "READY", record.get("error")
        names = {e["name"] for e in record["extraction"]["entities"]}
        assert {"示例登记", "测试服务局", "测试申请表"}.issubset(names)
        resolutions = {
            i["id"]: {"status": "verified", "note": "合成验收已对照标注核查，非真实政策"}
            for i in record["quality"]["issues"]
        }
        for action in ("approve", "publish"):
            response = client.post(
                f"/api/revisions/{rid}/review",
                headers=authorization,
                json={
                    "action": action,
                    "expected_version": record["version"],
                    "note": "合成标注验收，不是正式政策审核",
                    "issue_resolutions": resolutions if action == "approve" else {},
                },
            )
            assert response.status_code == 200, response.text
            record = response.json()
        graph = client.get("/api/graph", headers=authorization, params={"region": "测试甲区"}).json()
        assert graph["source"] == "openspg-neo4j" and graph["nodes"] and graph["edges"]
        result = client.post(
            "/api/query",
            headers=authorization,
            json={"question": "示例登记需要什么材料", "region": "测试甲区"},
        ).json()
        assert result["status"] == "ANSWERED"
        assert any(f["value"] == "测试申请表" for f in result["facts"])
        assert all(c["quote"] in record["text"] for c in result["citations"])
