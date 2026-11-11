"""目录适配离线契约，模型/图替身不表示真实抽取质量。"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from conftest import headers, publish
from hotline.directory import FIELDS, parse_directory
from hotline.models import Extraction
from hotline.settings import ServiceError

CSV = "事项名称,事项编码,适用地区,办理部门,收费标准,备注\n合成目录登记,TEST-A,测试甲区,合成服务局,免费,非真实政策\n合成目录登记,TEST-B,测试甲区,合成服务局,十元,非真实政策\n".encode()
MAPPING = {k: FIELDS[k] for k in ("service_name", "service_code", "region", "department", "fees")}


class EmptyRowExtractor:
    def __init__(self):
        self.seen = []

    def extract(self, chunks, metadata, revision_id, progress):
        self.seen.append(chunks)
        return Extraction(schema_version=2)


def test_directory_pipeline_identity_offsets_and_real_validation(application):
    extractor = EmptyRowExtractor()
    application.state.workflow.extractor = extractor
    with TestClient(application) as client:
        payload = {
            "region": "测试甲区",
            "source": "离线合成目录",
            "synthetic": True,
            "directory_mapping": MAPPING,
        }
        files = {"file": ("合成.csv", CSV, "text/csv")}
        assert (
            client.post("/api/import/directory/preview", headers=headers("agent"), files=files).status_code
            == 403
        )
        preview = client.post("/api/import/directory/preview", headers=headers(), files=files).json()
        assert preview["row_count"] == 2 and preview["sample"][0][1] == "TEST-A"
        first = client.post(
            "/api/import/directory", headers=headers(), files=files, data={"metadata": json.dumps(payload)}
        ).json()
        duplicate = client.post(
            "/api/import/directory", headers=headers(), files=files, data={"metadata": json.dumps(payload)}
        ).json()
        assert duplicate["duplicate"] and duplicate["revision_id"] == first["revision_id"]
        rid = first["revision_id"]
        application.state.workflow.process(application.state.store.claim())
        record = application.state.store.get_revision(rid)
        assert record["task_status"] == "READY", record["error"]
        assert record["knowledge_status"] == "DRAFT" and record["sha256"] == hashlib.sha256(CSV).hexdigest()
        assert len(extractor.seen) == 2 and all(len(c) == 1 for c in extractor.seen)
        assert all(c[0].previous is None and c[0].following is None for c in extractor.seen)
        services = [e for e in record["extraction"]["entities"] if e["type"] == "ServiceItem"]
        assert len(services) == 2 and len({e["id"] for e in services}) == 2
        assert {e["attributes"]["service_code"] for e in services} == {"TEST-A", "TEST-B"}
        chunk = record["chunks"][0]
        start = chunk["text"].index("TEST-A")
        located = client.get(
            f"/api/evidence/{rid}/{chunk['id']}/locate",
            headers=headers(),
            params={"start": start, "end": start + 6},
        ).json()
        assert located["regions"][0]["column"] == 2 and located["regions"][0]["row"] == 2
        assert located["regions"][0]["header"] == "事项编码"
        assert client.get(f"/api/source/{rid}", headers=headers()).content == CSV
        assert any(e["action"] == "BUILD_PROFILE" for e in application.state.store.history(rid))
        publish(client, rid)
        facts = client.post(
            "/api/query",
            headers=headers("agent"),
            json={"question": "合成目录登记由哪个部门办理？", "region": "测试甲区", "space": "test"},
        ).json()
        assert facts["status"] == "NEEDS_CLARIFICATION" and not facts["citations"]
        selected = client.post(
            "/api/retrieval/diagnose",
            headers=headers(),
            json={
                "question": "合成目录登记收费多少？",
                "region": "测试甲区",
                "service_code": "TEST-A",
                "space": "test",
            },
        ).json()
        assert selected["selected_sources"] and all(
            "TEST-B" not in s["quote"] for s in selected["selected_sources"]
        )
        assert selected["retrieval"]["candidate_count"] == 1
        assert len(client.get("/api/services?space=test", headers=headers()).json()["items"]) == 2


@pytest.mark.parametrize(
    "data,mapping,region",
    [
        (CSV.replace("测试甲区".encode(), "测试乙区".encode(), 1), MAPPING, "测试甲区"),
        (CSV.replace(b"TEST-B", b"TEST-A"), MAPPING, "测试甲区"),
        (CSV, {**MAPPING, "region": "事项名称"}, "测试甲区"),
        (CSV, {**MAPPING, "injected": "备注"}, "测试甲区"),
        (CSV, MAPPING, ""),
        (b"A,A,C\n1,2,3", MAPPING, "测试甲区"),
        (b"A,B,C\n1,2,3,4", MAPPING, "测试甲区"),
    ],
)
def test_directory_rejects_ambiguous_input(data, mapping, region):
    with pytest.raises(ServiceError):
        parse_directory(data, mapping, region, "r", 500000, 300)


def test_multiline_csv_keeps_exact_cell_text():
    data = '事项名称,事项编码,适用地区,备注\n合成事项,T1,测试甲区,"原件一份，\n另附复印件"\n'.encode()
    parsed = parse_directory(
        data, {k: FIELDS[k] for k in ("service_name", "service_code", "region")}, "测试甲区", "r", 500000, 300
    )
    cell = parsed.metadata["_source"]["pages"][0]["cells"][-1]
    assert parsed.text[cell["start"] : cell["end"]] == "原件一份，\n另附复印件"
    assert cell["csv_line_end"] == 3


def test_invalid_directory_does_not_create_task(application):
    with TestClient(application) as client:
        response = client.post(
            "/api/import/directory",
            headers=headers(),
            files={"file": ("x.csv", CSV)},
            data={"metadata": json.dumps({"region": "测试乙区", "directory_mapping": MAPPING})},
        )
        assert response.status_code == 400
        assert application.state.store.revision_page()["total"] == 0
