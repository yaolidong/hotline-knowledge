import io
import json

import pytest
from fastapi.testclient import TestClient
from docx import Document
from reportlab.pdfgen.canvas import Canvas

from conftest import headers, import_ready
from hotline.evidence import locate_evidence
from hotline.models import Chunk, Entity, Evidence, Extraction
from hotline.parsing import parse_document
from hotline.schema import DomainSchema
from hotline.semantics import quality_report, step_order
from hotline.settings import ServiceError


def extraction(text, kind, name, rule=None):
    chunk = Chunk(id="c", text=text, start=0, end=len(text))
    value = Extraction(
        schema_version=2,
        entities=[
            Entity(id="e", type=kind, name=name, rule=rule, evidence=[Evidence(chunk_id="c", quote=text)])
        ],
    )
    return chunk, value


def test_names_are_normalized_without_changing_evidence_or_semantics():
    chunk, value = extraction("依照食品经 营许可管理办法。", "LegalBasis", "食品经 营许可管理办法")
    value.entities[0].canonical_name = "伪造法律"
    output = DomainSchema().validate(value, [chunk]).entities[0]
    assert output.name == "食品经 营许可管理办法"
    assert output.canonical_name == "食品经营许可管理办法"
    assert output.evidence[0].quote == chunk.text
    value.entities[0].display_name = "全国食品审批"
    with pytest.raises(ServiceError, match="展示名称"):
        DomainSchema().validate(value, [chunk])


def test_material_alternatives_require_verbatim_operator_basis():
    text = "提交营业执照或者其他主体资格证明文件复印件。"
    rule = {
        "operator": "OR",
        "basis": "或者",
        "conditions": [{"condition": "营业执照"}, {"condition": "其他主体资格证明文件复印件"}],
    }
    chunk, value = extraction(text, "RequiredMaterial", text[:-1], rule)
    assert DomainSchema().validate(value, [chunk]).entities[0].rule["operator"] == "OR"
    value.entities[0].rule["operator"] = "AND"
    with pytest.raises(ServiceError, match="连接词"):
        DomainSchema().validate(value, [chunk])
    value.entities[0].rule = None
    assert "MATERIAL_ALTERNATIVE" in {x["code"] for x in quality_report(value)["issues"]}


def test_double_negation_rejected_and_unknown_logic_is_reviewable():
    chunk, value = extraction(
        "不得处于失信情形。",
        "EligibilityCondition",
        "不得处于失信情形",
        {"operator": "NOT", "basis": "不得", "conditions": [{"condition": "不得处于失信情形"}]},
    )
    with pytest.raises(ServiceError, match="双重否定"):
        DomainSchema().validate(value, [chunk])
    _, value = extraction("企业法人及自然人", "EligibilityCondition", "企业法人及自然人")
    assert "APPLICANT_AS_CONDITION" in {x["code"] for x in quality_report(value)["issues"]}
    assert step_order("（二）") == 2 and step_order("然后办理") is None


def test_flow_chain_has_evidence_without_inventing_a_number():
    from hotline.semantics import entity_sequence

    chunk, value = extraction("办理流程：申请-受理-审批（现场核查）-决定-发证", "ProcedureStep", "受理")
    value.entities[0].sequence_basis = "申请-受理-审批（现场核查）-决定-发证"
    entity = DomainSchema().validate(value, [chunk]).entities[0]
    assert entity_sequence(entity) == 2 and not entity.attributes.get("order")
    value.entities[0].sequence_basis = "申请-发证-受理"
    with pytest.raises(ServiceError, match="流程顺序"):
        DomainSchema().validate(value, [chunk])


def test_literal_normalization_reclassifies_without_losing_evidence():
    from hotline.models import Relation
    from hotline.normalization import normalize_extraction
    from hotline.semantics import entity_sequence

    text = "甲事项。申请条件：企业法人及自然人。材料：甲证明或者乙证明复印件。办理流程：申请-受理-决定。"
    chunk = Chunk(id="c", text=text, start=0, end=len(text))
    defs = [
        ("s", "ServiceItem", "甲事项"),
        ("c", "EligibilityCondition", "企业法人及自然人"),
        ("m", "RequiredMaterial", "甲证明或者乙证明复印件"),
        ("p", "ProcedureStep", "受理"),
        ("p1", "ProcedureStep", "申请"),
        ("p2", "ProcedureStep", "决定"),
    ]
    value = Extraction(
        entities=[
            Entity(id=key, type=kind, name=name, evidence=[Evidence(chunk_id="c", quote=text)])
            for key, kind, name in defs
        ],
        relations=[
            Relation(
                source="s",
                target="c",
                type="REQUIRES_CONDITION",
                evidence=[Evidence(chunk_id="c", quote=text)],
            )
        ],
    )
    value = DomainSchema().validate(value, [chunk])
    result = DomainSchema().validate(normalize_extraction(value, [chunk]), [chunk])
    assert not any(e.id == "c" for e in result.entities)
    assert result.entities[0].attributes["applicant_scope"] == "企业法人及自然人"
    material = next(e for e in result.entities if e.id == "m")
    assert material.rule["conditions"][1]["condition"] == "乙证明复印件"
    assert entity_sequence(next(e for e in result.entities if e.id == "p")) == 2
    assert {x["kind"] for x in result.normalizations} == {
        "APPLICANT_CATEGORY",
        "MATERIAL_OR",
        "EXPLICIT_FLOW",
    }
    assert all(e.evidence[0].quote == text for e in result.entities)


def test_ambiguous_material_and_qualified_subject_are_not_rewritten():
    from hotline.normalization import normalize_extraction

    chunk, value = extraction(
        "提交甲证明或者乙证明，并且提交丙证明", "RequiredMaterial", "甲证明或者乙证明，并且提交丙证明"
    )
    assert normalize_extraction(value, [chunk]).entities[0].rule is None
    chunk, value = extraction("年满十八周岁的自然人", "EligibilityCondition", "年满十八周岁的自然人")
    assert normalize_extraction(value, [chunk]).entities[0].type == "EligibilityCondition"


def test_quality_acknowledgement_is_authorized_and_audited(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        record = application.state.store.get_revision(rid)
        changed = {**record["extraction"], "schema_version": 2}
        response = client.put(
            f"/api/revisions/{rid}/extraction",
            headers=headers(),
            json={"expected_version": record["version"], "note": "合成语义测试", "extraction": changed},
        )
        assert response.status_code == 200
        record = client.get(f"/api/revisions/{rid}", headers=headers()).json()
        body = {"action": "approve", "expected_version": record["version"], "note": "核对合成资料"}
        assert (
            client.post(f"/api/revisions/{rid}/review", headers=headers("reviewer"), json=body).status_code
            == 409
        )
        body["acknowledged_issues"] = [x["id"] for x in record["quality"]["issues"]]
        assert (
            client.post(f"/api/revisions/{rid}/review", headers=headers("agent"), json=body).status_code
            == 403
        )
        assert (
            client.post(f"/api/revisions/{rid}/review", headers=headers("reviewer"), json=body).status_code
            == 200
        )
        history = application.state.store.history(rid)
        assert any(
            x["action"] == "APPROVE" and json.loads(x["snapshot"])["acknowledged_issues"] for x in history
        )


def record_for(parsed):
    return {"id": "r", "document_id": "d", "sha256": "hash", "number": 1, "parsed_metadata": parsed.metadata}


def test_issue_resolutions_block_unresolved_and_keep_reasons(application):
    with TestClient(application) as client:
        rid = import_ready(client, application)
        record = application.state.store.get_revision(rid)
        changed = {**record["extraction"], "schema_version": 2}
        client.put(
            f"/api/revisions/{rid}/extraction",
            headers=headers(),
            json={"expected_version": record["version"], "note": "合成质检", "extraction": changed},
        ).raise_for_status()
        record = client.get(f"/api/revisions/{rid}", headers=headers()).json()
        issues = [x["id"] for x in record["quality"]["issues"]]
        assert issues
        body = {
            "action": "approve",
            "expected_version": record["version"],
            "note": "逐项合成核对",
            "acknowledged_issues": issues,
            "issue_resolutions": {key: {"status": "unresolved", "note": "尚待核对"} for key in issues},
        }
        assert client.post(f"/api/revisions/{rid}/review", headers=headers(), json=body).status_code == 409
        body["issue_resolutions"] = {
            key: {"status": "verified", "note": "已核对合成原文中的完整要求"} for key in issues
        }
        first_issue = issues[0]
        body["issue_resolutions"][first_issue]["note"] = ""
        assert client.post(f"/api/revisions/{rid}/review", headers=headers(), json=body).status_code == 422
        body["issue_resolutions"][first_issue]["note"] = "   "
        assert client.post(f"/api/revisions/{rid}/review", headers=headers(), json=body).status_code == 409
        assert application.state.store.get_revision(rid)["knowledge_status"] == "DRAFT"
        assert not application.state.graph.graphs
        body["issue_resolutions"][first_issue]["note"] = "已核对合成原文中的完整要求"
        body["issue_resolutions"]["stale-issue"] = {"status": "false_positive", "note": "过期问题"}
        assert client.post(f"/api/revisions/{rid}/review", headers=headers(), json=body).status_code == 409
        del body["issue_resolutions"]["stale-issue"]
        assert (
            client.post(f"/api/revisions/{rid}/review", headers=headers("reviewer"), json=body).status_code
            == 200
        )
        event = next(e for e in application.state.store.history(rid) if e["action"] == "APPROVE")
        snapshot = json.loads(event["snapshot"])
        assert (
            snapshot["resolution_mode"] == "per_issue"
            and snapshot["issue_resolutions"] == body["issue_resolutions"]
        )


def test_docx_cell_locator_keeps_row_column_and_exact_offsets():
    doc = Document()
    table = doc.add_table(rows=3, cols=2)
    for row, values in zip(table.rows, [("材料", "份数"), ("甲材料", "一份"), ("乙材料", "一份")]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    buf = io.BytesIO()
    doc.save(buf)
    parsed = parse_document(buf.getvalue(), ".docx", "r")
    chunk = parsed.chunks[0].model_dump()
    start = chunk["text"].rindex("一份")
    result = locate_evidence(record_for(parsed), chunk, start, start + 2)
    assert result["regions"][0]["row"] == 3
    assert result["regions"][0]["column"] == 2
    assert len(result["regions"]) == 1


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_native_pdf_regions_support_rotated_pages(rotation):
    from pypdf import PdfReader, PdfWriter

    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=(400, 300))
    canvas.drawString(40, 240, "Evidence Alpha")
    canvas.save()
    writer = PdfWriter()
    writer.add_page(PdfReader(buf).pages[0].rotate(rotation))
    out = io.BytesIO()
    writer.write(out)
    parsed = parse_document(out.getvalue(), ".pdf", "r")
    chunk = parsed.chunks[0].model_dump()
    start = chunk["text"].index("Evidence Alpha")
    result = locate_evidence(record_for(parsed), chunk, start, start + len("Evidence Alpha"))
    assert result["status"] == "LOCATED"
    assert all(0 <= n <= 1 for r in result["regions"] for p in r["box"] for n in p)
    # 文字原本位于左上方；旋转后中心必须落在相应象限，防止框与渲染图错位。
    points = result["regions"][0]["box"]
    x, y = [sum(p[i] for p in points) / 4 for i in (0, 1)]
    assert (x < 0.5, y < 0.5) == {
        0: (True, True),
        90: (False, True),
        180: (False, False),
        270: (True, False),
    }[rotation]


def test_repeated_ocr_text_uses_offsets_not_text_similarity():
    text = "一份\n一份"
    chunk = {"id": "c", "start": 0, "page": 1, "text": text}
    record = {
        "id": "r",
        "document_id": "d",
        "sha256": "hash",
        "number": 1,
        "parsed_metadata": {
            "_source": {
                "pages": [
                    {
                        "page": 1,
                        "start": 0,
                        "method": "ocr",
                        "lines": [
                            {"text": "一份", "start": 0, "end": 2, "box": []},
                            {"text": "一份", "start": 3, "end": 5, "box": []},
                        ],
                    }
                ]
            }
        },
    }
    result = locate_evidence(record, chunk, 3, 5)
    assert len(result["regions"]) == 1 and result["regions"][0]["absolute_start"] == 3
