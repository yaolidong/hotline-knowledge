import subprocess
import sys
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import import_ready
from hotline.graph import GraphStore, build_subgraph, unpack_node
from hotline.schema import DomainSchema, spg_name
from hotline.settings import ServiceError


def test_schema_parses_with_real_kag_and_has_typed_relations():
    from knext.schema.marklang.schema_ml import SPGSchemaMarkLang

    parsed = SPGSchemaMarkLang("", with_server=False, script_data_str=DomainSchema().spg_schema("Hotline"))
    assert len(parsed.types) == 12
    assert "Hotline.ServiceItem" in parsed.types
    assert any(r.name == "requiresMaterial" for r in parsed.types["Hotline.ServiceItem"].relations.values())
    assert spg_name("SUPPORTED_BY") == "supportedBy"


def test_kag_import_in_fresh_process():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kag.interface.common.model.sub_graph import SubGraph; from kag.common.llm.openai_client import OpenAIClient",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_kag_subgraph_roundtrip_evidence_and_stable_ids(application):
    from kag.interface.common.model.sub_graph import SubGraph

    with TestClient(application) as client:
        rid = import_ready(client, application)
        record = application.state.store.get_revision(rid)
        graph = build_subgraph(record, "Hotline")
        assert SubGraph.from_dict(graph).to_dict() == graph
        assert build_subgraph(record, "Hotline") == graph
        assert all(n["label"].startswith("Hotline.") for n in graph["resultNodes"])
        assert "requiresMaterial" in {e["label"] for e in graph["resultEdges"]}
        assert all(e["properties"]["recordId"] == rid for e in graph["resultEdges"])
        assert all(e["properties"]["contentHash"] for e in graph["resultEdges"])


def test_openspg_contract_checks_success_envelope(application, settings, monkeypatch):
    settings.openspg_project_id = 12
    graph = GraphStore(settings)
    monkeypatch.setattr(graph, "health", lambda: True)
    seen = []

    def post(url, json, timeout, trust_env):
        seen.append((url, json))
        return httpx.Response(
            200, json={"success": False, "result": False}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("hotline.graph.httpx.post", post)
    with TestClient(application) as client:
        rid = import_ready(client, application)
        with pytest.raises(ServiceError, match="未确认"):
            graph.publish(application.state.store.get_revision(rid))
    assert seen[0][0].endswith("/public/v1/graph/writerGraph")
    assert seen[0][1]["projectId"] == 12
    assert seen[0][1]["operation"] == "UPSERT"
    assert seen[0][1]["enableLeadTo"] is False


def test_readback_detects_missing_edges(application, settings, monkeypatch):
    graph = GraphStore(settings)
    with TestClient(application) as client:
        rid = import_ready(client, application)
        payload = build_subgraph(application.state.store.get_revision(rid), "Hotline")

    def read(cypher, **params):
        if "MATCH (n)" in cypher:
            return [{"id": n["id"], "hash": n["properties"]["contentHash"]} for n in payload["resultNodes"]]
        return []

    monkeypatch.setattr(graph, "_read", read)
    with pytest.raises(ServiceError, match="回读不一致"):
        graph.verify(rid, payload)


def test_readback_checks_all_nodes_and_edges(application, settings, monkeypatch):
    graph = GraphStore(settings)
    with TestClient(application) as client:
        rid = import_ready(client, application)
        payload = build_subgraph(application.state.store.get_revision(rid), "Hotline")

    def read(cypher, **params):
        if "MATCH (n)" in cypher:
            return [{"id": n["id"], "hash": n["properties"]["contentHash"]} for n in payload["resultNodes"]]
        return [
            {"id": e["properties"]["factId"], "hash": e["properties"]["contentHash"]}
            for e in payload["resultEdges"]
        ]

    monkeypatch.setattr(graph, "_read", read)
    graph.verify(rid, payload)


def test_user_text_is_never_interpolated_into_cypher(settings, monkeypatch):
    graph = GraphStore(settings)
    malicious = "' MATCH (n) DETACH DELETE n //"
    calls = []

    def read(cypher, **params):
        calls.append((cypher, params))
        return []

    monkeypatch.setattr(graph, "_read", read)
    graph.browse(["r"], keyword=malicious, region=malicious)
    graph.services(["r"], malicious, service=malicious)
    assert all(malicious not in query for query, _ in calls)
    assert any(malicious in params.values() for _, params in calls)


def test_openspg_json_scalar_storage_is_decoded_without_changing_business_values(settings, monkeypatch):
    properties = {
        "id": "node-id",
        "recordId": "revision-id",
        "entityType": "ServiceItem",
        "name": '测试"事项"',
        "region": "测试甲区",
        "aliases": json.dumps(["测试别名"], ensure_ascii=False),
        "evidence": json.dumps([{"quote": "第一条 测试事项。"}], ensure_ascii=False),
        "attrs": "{}",
    }
    stored = {
        key: value if key == "id" else json.dumps(value, ensure_ascii=False)
        for key, value in properties.items()
    }
    decoded = unpack_node(stored)
    assert decoded["name"] == '测试"事项"'
    assert decoded["recordId"] == "revision-id" and decoded["id"] == "node-id"
    assert decoded["evidence"][0]["quote"] == "第一条 测试事项。"
    assert decoded["aliases"] == ["测试别名"]
    graph = GraphStore(settings)

    def read(cypher, **params):
        assert params["records"] == ['"revision-id"']
        assert params["region"] == '"测试甲区"'
        return [{"node": stored}]

    monkeypatch.setattr(graph, "_read", read)
    nodes, _ = graph.services(["revision-id"], '测试"事项"需要什么材料', region="测试甲区")
    assert nodes[0]["name"] == '测试"事项"'
