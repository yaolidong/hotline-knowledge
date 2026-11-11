from fastapi.testclient import TestClient

from conftest import TOKENS, headers, import_ready, publish
from hotline.app import create_app
from hotline.rate_limit import TokenBucketLimiter
from hotline.settings import Settings


def test_token_bucket_blocks_burst():
    limiter = TokenBucketLimiter(rate_per_minute=60, burst=2)
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert limiter.allow("a") is False


def test_rate_limit_returns_429(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        default_knowledge_space="test",
        worker_enabled=False,
        rate_limit_enabled=True,
        rate_limit_per_minute=60,
        rate_limit_burst=1,
        users={token: {"name": role, "role": role} for role, token in TOKENS.items()},
    )
    from conftest import RecordingGraph

    app = create_app(settings, graph=RecordingGraph())
    with TestClient(app) as client:
        first = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "region": "测试甲区", "space": "test"},
        )
        second = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "示例登记需要什么材料", "region": "测试甲区", "space": "test"},
        )
        assert first.status_code == 200
        assert second.status_code == 429


def test_acl_agent_cannot_download_unpublished_or_list_feedback(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        draft = client.get(f"/api/source/{rid}", headers=headers("agent"), params={"space": "test"})
        assert draft.status_code == 403
        draft_text = client.get(f"/api/source/{rid}/text", headers=headers("agent"), params={"space": "test"})
        assert draft_text.status_code == 403
        maintainer = client.get(f"/api/source/{rid}/text", headers=headers("maintainer"), params={"space": "test"})
        assert maintainer.status_code == 200
        publish(client, rid)
        published = client.get(f"/api/source/{rid}", headers=headers("agent"), params={"space": "test"})
        assert published.status_code == 200
        feedback = client.get("/api/feedback", headers=headers("agent"))
        assert feedback.status_code == 403
        listed = client.get("/api/feedback?space=test", headers=headers("maintainer"))
        assert listed.status_code == 200


def test_health_includes_checks(application):
    with TestClient(application) as client:
        body = client.get("/health").json()
        assert body["service"] == "hotline-knowledge"
        assert "checks" in body
        assert "model_configured" in body["checks"]
        assert "graph_reachable" in body["checks"]


def test_audit_export_maintainer_only(application):
    with TestClient(application) as client:
        assert client.get("/api/operations/audit-export", headers=headers("agent")).status_code == 403
        body = client.get("/api/operations/audit-export?limit=10", headers=headers("maintainer")).json()
        assert "items" in body
        assert body["limit"] == 10
