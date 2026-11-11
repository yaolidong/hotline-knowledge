"""评测边界测试使用合成标注；不作为真实政策质量成绩。"""

import copy

import pytest
from pydantic import ValidationError

from conftest import fixture_extraction, guide
from hotline.evaluation import FactProbe, QueryProbe, RegressionSuite, evaluate_answer, evaluate_facts
from hotline.parsing import parse_document
from hotline.settings import ServiceError


def example():
    parsed = parse_document(guide().encode(), ".txt", "r")
    record = {
        "id": "r",
        "number": 1,
        "sha256": "a" * 64,
        "knowledge_status": "PUBLISHED",
        "metadata": {"region": "测试甲区", "synthetic": False},
        "text": parsed.text,
        "chunks": [c.model_dump() for c in parsed.chunks],
        "extraction": fixture_extraction(parsed.chunks).model_dump(),
    }
    suite = RegressionSuite(
        source_sha256="a" * 64,
        service="示例登记",
        region="测试甲区",
        source_kind="DERIVED_TEXT",
        facts=[
            FactProbe(
                id="material",
                label="材料",
                entity_type="RequiredMaterial",
                text="测试申请表",
                anchor="第二条 申请材料：测试申请表。",
                relation="REQUIRES_MATERIAL",
            )
        ],
    )
    return record, suite


def test_fact_probe_checks_relationship_and_does_not_create_quality_metric():
    record, suite = example()
    report = evaluate_facts(record, suite)
    assert report["checks"][0]["status"] == "PASSED" and report["quality_metrics"] is None
    assert report["annotation_status"] == "PENDING_HUMAN_REVIEW"
    record["extraction"]["relations"] = [
        r for r in record["extraction"]["relations"] if r["type"] != "REQUIRES_MATERIAL"
    ]
    assert evaluate_facts(record, suite)["checks"][0]["status"] == "NEEDS_REVIEW"


@pytest.mark.parametrize("change", ["hash", "region", "synthetic", "anchor", "invalid_evidence"])
def test_fact_probes_reject_wrong_or_corrupt_sources(change):
    record, suite = example()
    if change == "hash":
        record["sha256"] = "b" * 64
    if change == "region":
        record["metadata"]["region"] = "测试乙区"
    if change == "synthetic":
        record["metadata"]["synthetic"] = True
    if change == "anchor":
        suite.facts[0].anchor = "原文不存在"
    if change == "invalid_evidence":
        record["extraction"]["entities"][0]["evidence"][0]["start"] = 100000
    with pytest.raises(ServiceError):
        evaluate_facts(record, suite)


def test_probe_definition_rejects_unknown_fields_and_duplicate_ids():
    _, suite = example()
    value = suite.model_dump()
    value["facts"].append(copy.deepcopy(value["facts"][0]))
    with pytest.raises(ValidationError):
        RegressionSuite.model_validate(value)
    value["facts"] = value["facts"][:1]
    value["facts"][0]["entity_type"] = "UnknownType"
    with pytest.raises(ValidationError):
        RegressionSuite.model_validate(value)


@pytest.mark.parametrize(
    "change", ["none", "negative_offset", "wrong_quote", "unpublished", "wrong_hash", "different_region"]
)
def test_answer_evidence_uses_exact_current_published_source(change):
    record, suite = example()
    chunk = next(c for c in record["chunks"] if "测试申请表" in c["text"])
    quote = "测试申请表"
    left = chunk["text"].index(quote)
    citation = {
        "revision_id": "r",
        "chunk_id": chunk["id"],
        "start": left,
        "end": left + len(quote),
        "quote": quote,
    }
    answer = {
        "status": "ANSWERED",
        "answer": "需要测试申请表",
        "citations": [citation],
        "generation": {"used": True},
    }
    probe = QueryProbe(
        id="q",
        label="材料",
        mode="qa",
        request={"question": "材料是什么"},
        allowed_statuses=["ANSWERED"],
        required_answer_fragments=["测试申请表"],
        required_citation_fragments=["测试申请表"],
        require_model=True,
    )
    if change == "negative_offset":
        citation["start"] = -1
    if change == "wrong_quote":
        citation["quote"] = "编造材料"
    if change == "unpublished":
        record["knowledge_status"] = "DRAFT"
    if change == "wrong_hash":
        record["sha256"] = "b" * 64
    if change == "different_region":
        record["metadata"]["region"] = "测试乙区"
    assert evaluate_answer(answer, probe, {"r": record}, suite)["passed"] == (change == "none")
