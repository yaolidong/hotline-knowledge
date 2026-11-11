from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import headers, import_ready, publish
from hotline.embedding import SemanticRetriever
from hotline.models import Question
from hotline.temporal import query_date
from hotline.settings import ServiceError


@pytest.mark.parametrize(
    "question,explicit,source,selected",
    [
        ("截至2024年3月1日办理需要哪些材料", "", "QUESTION", "2024-03-01"),
        ("2024-03-01还能申请吗", "2025-03-01", "CONFLICT", "2025-03-01"),
        ("2024年申请条件", "", "AMBIGUOUS", "2026-11-04"),
        ("2024年3月1日发布的通知需要哪些材料", "", "TODAY", "2026-11-04"),
        ("去年还能办理吗", "", "AMBIGUOUS", "2026-11-04"),
        ("2025年2月30日是否有效", "", "INVALID", "2026-11-04"),
        ("2023-01-01和2024-01-01要求有什么区别", "", "AMBIGUOUS", "2026-11-04"),
    ],
)
def test_query_date_never_silently_defaults_historical_questions(question, explicit, source, selected):
    resolved, method, message = query_date(question, explicit, date(2026, 11, 4))
    assert method == source and resolved.isoformat() == selected
    assert bool(message) == (source in {"CONFLICT", "AMBIGUOUS", "INVALID"})


def test_ambiguous_date_never_calls_model(application):
    class NoModel:
        def complete(self, messages):
            raise AssertionError("不能为日期歧义生成答案")

    application.state.qa.model = NoModel()
    with TestClient(application) as client:
        publish(client, import_ready(client, application))
        result = client.post(
            "/api/qa", headers=headers("agent"), json={"question": "去年示例登记需要什么材料"}
        ).json()
        assert result["status"] == "NEEDS_CLARIFICATION" and not result["generation"]["used"]


def test_embedding_cache_and_invalid_vectors(settings):
    settings.embedding_base_url, settings.embedding_model = "http://local.test/v1", "test-only"
    retriever = SemanticRetriever(settings)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=[1.0, 0.5]) for i, _ in enumerate(kwargs["input"])]
        )

    retriever.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    assert retriever.rank("测试问题", ["证据甲", "证据乙"])[0][0] == 0
    retriever.rank("测试问题", ["证据甲", "证据乙"])
    assert len(calls) == 1
    with pytest.raises(ServiceError):
        retriever.validate([float("nan")])


def test_semantic_only_receives_published_scoped_evidence(application):
    seen = []

    class Ranking:
        enabled = True

        def rank(self, question, texts):
            seen.extend(texts)
            return [(i, 0.8) for i in range(len(texts))]

    application.state.qa.semantic = Ranking()
    with TestClient(application) as client:
        public = import_ready(client, application)
        publish(client, public)
        application.state.workflow.extractor.material = "不应进入模型的草稿材料"
        from conftest import guide

        import_ready(client, application, text=guide("不应进入模型的草稿材料"), title="草稿")
        request = Question(question="示例登记需要什么材料", region="测试甲区")
        result = application.state.qa.graph_query.ask(request)
        sources, _ = application.state.qa._retrieve(request, result)
        assert sources and seen
        assert not any("不应进入模型" in text for text in seen)
        assert result["retrieval"]["original_text"] == "GRAPH_KEYWORD_VECTOR_RRF"


def test_all_graph_evidence_survives_retrieval_limit_and_context_overflow_is_explicit(
    application, monkeypatch
):
    # 离线编排回归：12 个独立图证据块，不能被普通召回前 8 项截断。
    chunks = [
        {
            "id": str(i),
            "text": f"合成条件{i}必须核对。",
            "start": i * 30,
            "page": None,
            "section": "",
            "previous": None,
            "following": None,
        }
        for i in range(12)
    ]
    record = {
        "id": "published",
        "document_id": "document",
        "filename": "测试.txt",
        "knowledge_status": "PUBLISHED",
        "metadata": {"region": "测试甲区", "synthetic": True},
        "chunks": chunks,
        "extraction": {"entities": [], "relations": []},
    }
    service = application.state.qa
    monkeypatch.setattr(service.store, "visible_ids", lambda **kwargs: [record["id"]])
    monkeypatch.setattr(service.store, "get_revision", lambda _: record)
    result = {
        "services": [],
        "warnings": [],
        "as_of": "2026-11-04",
        "citations": [{"revision_id": record["id"], "chunk_id": c["id"]} for c in chunks],
    }
    request = Question(question="需要满足哪些条件", region="测试甲区")
    sources, clarification = service._retrieve(request, result)
    assert not clarification and {s["chunk_id"] for s in sources} == {c["id"] for c in chunks}
    service.settings.qa_context_chars = 10
    sources, clarification = service._retrieve(request, result)
    assert not sources and "上限" in clarification


def test_evidence_locator_enforces_publication_and_bounds(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        chunk = application.state.store.get_revision(rid)["chunks"][0]
        url = f"/api/evidence/{rid}/{chunk['id']}/locate"
        span = {"start": 0, "end": 2}
        assert client.get(url, params=span).status_code == 401
        assert client.get(url, headers=headers("agent"), params=span).status_code == 403
        assert client.get(url, headers=headers("reviewer"), params=span).status_code == 200
        publish(client, rid)
        response = client.get(url, headers=headers("agent"), params=span)
        assert response.status_code == 200 and response.json()["quote"] == chunk["text"][:2]
        for start, end, status in [(-1, 2, 422), (2, 1, 400), (0, len(chunk["text"]) + 1, 400)]:
            assert (
                client.get(url, headers=headers("agent"), params={"start": start, "end": end}).status_code
                == status
            )
