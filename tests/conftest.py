"""测试替身只用于离线契约/业务验证，不代表真实 LLM 或图服务集成成功。"""

import pytest

from hotline.app import create_app
from hotline.graph import build_subgraph, unpack_node
from hotline.models import Entity, Evidence, Extraction, Relation
from hotline.schema import DomainSchema
from hotline.settings import ServiceError, Settings

TOKENS = {role: role + "-" + "x" * 32 for role in ("admin", "maintainer", "reviewer", "agent")}


def guide(material="测试申请表", region="测试甲区"):
    return f"""合成测试资料，不是真实政策，不得用于实际办理。
{region}示例登记办事指南
第一条 事项名称：示例登记。办理部门：测试服务局。
第二条 申请材料：{material}。
第三条 申请条件：同时满足年满十八周岁、居住在测试甲区，并且不属于失信情形。
第四条 办理流程：提交申请，然后窗口核验。
第五条 办理渠道：测试线上平台。
第六条 政策依据：《合成示例管理规定》第一条。
"""


def fixture_extraction(chunks, material="测试申请表"):
    """经过人工定义的合成标注，不以此衡量真实模型质量。"""

    def ev(term):
        chunk = next(c for c in chunks if term in c.text)
        return Evidence(chunk_id=chunk.id, quote=chunk.text, start=0, end=len(chunk.text))

    definitions = [
        ("s", "ServiceItem", "示例登记"),
        ("d", "GovernmentDepartment", "测试服务局"),
        ("m", "RequiredMaterial", material),
        ("c", "EligibilityCondition", "同时满足年满十八周岁、居住在测试甲区，并且不属于失信情形"),
        ("p", "ProcedureStep", "提交申请，然后窗口核验"),
        ("h", "ServiceChannel", "测试线上平台"),
        ("l", "LegalBasis", "《合成示例管理规定》第一条"),
    ]
    entities = [Entity(id=key, type=kind, name=name, evidence=[ev(name)]) for key, kind, name in definitions]
    entities[3].rule = {
        "operator": "AND",
        "conditions": [
            {"condition": "年满十八周岁"},
            {"condition": "居住在测试甲区"},
            {"operator": "NOT", "conditions": [{"condition": "失信情形"}]},
        ],
    }
    relations = [
        Relation(source="s", target=key, type=kind, evidence=[ev(entities[index].name)])
        for index, (key, kind) in enumerate(
            [
                ("d", "HANDLED_BY"),
                ("m", "REQUIRES_MATERIAL"),
                ("c", "REQUIRES_CONDITION"),
                ("p", "HAS_PROCEDURE"),
                ("h", "HAS_CHANNEL"),
                ("l", "BASED_ON"),
            ],
            1,
        )
    ]
    return DomainSchema().validate(Extraction(entities=entities, relations=relations), chunks)


class AnnotatedExtractor:
    """从测试标注构造结果，禁止在应用运行时使用。"""

    material = "测试申请表"

    def extract(self, chunks, metadata, revision_id, progress):
        progress(len(chunks), len(chunks))
        return fixture_extraction(chunks, self.material)


class RecordingGraph:
    """内存契约替身：检查发布内容及查询编排，不代表 Neo4j 集成。"""

    def __init__(self):
        self.graphs = {}
        self.fail = False

    def publish(self, record):
        if self.fail:
            raise ServiceError("模拟图写入故障", 502)
        self.graphs[record["id"]] = build_subgraph(record, "Hotline")

    def health(self):
        return True

    def close(self):
        pass

    def nodes(self, record_ids):
        return [
            unpack_node({"id": n["id"], "name": n["name"], **n["properties"]})
            for rid in record_ids
            for n in self.graphs.get(rid, {}).get("resultNodes", [])
        ]

    def services(self, record_ids, question, region="", service=""):
        return [
            n
            for n in self.nodes(record_ids)
            if n["entityType"] == "ServiceItem"
            and (not region or n["region"] == region)
            and ((service and service == n["name"]) or (not service and n["name"] in question))
        ], False

    def service_candidates(self, record_ids):
        return [n for n in self.nodes(record_ids) if n["entityType"] == "ServiceItem"], False

    def facts(self, record_ids, service_ids):
        nodes = {n["id"]: n for n in self.nodes(record_ids)}
        return [
            {
                "source": nodes[e["from"]],
                "target": nodes[e["to"]],
                "type": e["properties"]["relationType"],
                "relation": unpack_node(e["properties"]),
            }
            for rid in record_ids
            for e in self.graphs.get(rid, {}).get("resultEdges", [])
            if e["from"] in service_ids and e["properties"]["relationType"] != "SUPPORTED_BY"
        ], False

    def browse(self, record_ids, **kwargs):
        return {"nodes": self.nodes(record_ids), "edges": [], "has_more": False, "source": "TEST_DOUBLE"}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        default_knowledge_space="test",
        worker_enabled=False,
        rate_limit_enabled=False,
        users={token: {"name": role, "role": role} for role, token in TOKENS.items()},
    )


@pytest.fixture
def application(settings):
    return create_app(settings, extractor=AnnotatedExtractor(), graph=RecordingGraph())


def headers(role="admin"):
    return {"Authorization": "Bearer " + TOKENS[role]}


def import_ready(client, app, text=None, region="测试甲区", title="合成测试指南"):
    result = client.post(
        "/api/import/text",
        headers=headers(),
        json={"text": text or guide(), "title": title, "region": region, "synthetic": True},
    ).json()
    rid = result["revision_id"]
    app.state.workflow.process(app.state.store.claim())
    record = app.state.store.get_revision(rid)
    assert record["task_status"] == "READY", record["error"]
    return rid


def publish(client, rid):
    for action in ("approve", "publish"):
        version = client.get(f"/api/revisions/{rid}", headers=headers()).json()["version"]
        response = client.post(
            f"/api/revisions/{rid}/review",
            headers=headers("reviewer"),
            json={"action": action, "expected_version": version, "note": "测试人工标注审核"},
        )
        assert response.status_code == 200, response.text
