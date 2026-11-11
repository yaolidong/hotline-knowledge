"""针对固定来源的工程回归核对；局部清单不作为完整人工真值或准确率。"""

import re
from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from hotline.models import Chunk, Entity, Extraction, Question, StrictModel
from hotline.schema import DomainSchema
from hotline.semantics import entity_sequence
from hotline.settings import ServiceError


def compact(text):
    return re.sub(r"\s+", "", text)


class FactProbe(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    label: str
    entity_type: str
    text: str = Field(min_length=1)
    anchor: str = Field(min_length=1)
    relation: str = ""
    attribute: str = ""
    attribute_value: str = ""
    operator: Literal["", "AND", "OR", "NOT"] = ""
    sequence: int | None = Field(None, ge=1)
    absent: bool = False

    @model_validator(mode="after")
    def supported_selector(self):
        schema = DomainSchema()
        if self.entity_type not in schema.entities or (
            self.relation and self.relation not in schema.relations
        ):
            raise ValueError("核对清单含未知实体/关系类型")
        if self.attribute and self.attribute not in schema.entities[self.entity_type]["attributes"]:
            raise ValueError("核对清单含未知属性")
        if bool(self.attribute) != bool(self.attribute_value):
            raise ValueError("属性与原文值必须同时指定")
        return self


class QueryProbe(StrictModel):
    id: str
    label: str
    mode: Literal["query", "qa"]
    request: Question
    allowed_statuses: list[str] = Field(min_length=1)
    required_answer_fragments: list[str] = Field(default_factory=list)
    required_citation_fragments: list[str] = Field(default_factory=list)
    no_citations: bool = False
    require_model: bool = False


class RegressionSuite(StrictModel):
    version: Literal[1] = 1
    annotation_status: Literal["PENDING_HUMAN_REVIEW"] = "PENDING_HUMAN_REVIEW"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service: str
    region: str
    source_kind: Literal["DERIVED_TEXT", "ORIGINAL_DOCUMENT"]
    facts: list[FactProbe] = Field(min_length=1, max_length=200)
    questions: list[QueryProbe] = Field(default_factory=list, max_length=100)
    human_review_tasks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        for items in (self.facts, self.questions):
            if len({item.id for item in items}) != len(items):
                raise ValueError("核对项 ID 不得重复")
        return self


def check_source(record, suite):
    if record["sha256"] != suite.source_sha256 or record["metadata"].get("region") != suite.region:
        raise ServiceError("来源哈希或地区与固定核对清单不一致，拒绝套用旧预期", 409)
    if not record.get("extraction") or not record.get("text"):
        raise ServiceError("版本尚无完整抽取及原文，不能核对", 409)
    if record["metadata"].get("synthetic"):
        raise ServiceError("真实资料清单不能套用合成来源", 409)
    DomainSchema().validate(
        Extraction.model_validate(record["extraction"]), [Chunk.model_validate(c) for c in record["chunks"]]
    )


def evaluate_facts(record, suite):
    check_source(record, suite)
    entities = record["extraction"]["entities"]
    services = {
        e["id"]
        for e in entities
        if e["type"] == "ServiceItem" and compact(e["name"]) == compact(suite.service)
    }
    results = []
    for probe in suite.facts:
        if probe.anchor not in record["text"]:
            raise ServiceError(f"核对项 {probe.id} 的依据未逐字出现在该来源，必须重新审阅清单", 409)
        targets = {
            r["target"]
            for r in record["extraction"]["relations"]
            if r["type"] == probe.relation and r["source"] in services
        }
        matches = [
            e
            for e in entities
            if e["type"] == probe.entity_type
            and compact(probe.text) in compact(e["name"])
            and (not probe.relation or e["id"] in targets)
            and (
                not probe.attribute
                or compact(probe.attribute_value) in compact(e["attributes"].get(probe.attribute, ""))
            )
            and (not probe.operator or (e.get("rule") or {}).get("operator") == probe.operator)
            and (probe.sequence is None or entity_sequence(Entity.model_validate(e)) == probe.sequence)
        ]
        locations = []
        start = 0
        while (offset := record["text"].find(probe.anchor, start)) >= 0:
            locations.append({"start": offset, "end": offset + len(probe.anchor)})
            start = offset + len(probe.anchor)
        results.append(
            {
                "id": probe.id,
                "label": probe.label,
                "status": "PASSED" if bool(matches) != probe.absent else "NEEDS_REVIEW",
                "matched_entity_ids": [e["id"] for e in matches],
                "anchor": probe.anchor,
                "source_offsets": locations,
            }
        )
    return {
        "revision_id": record["id"],
        "version": record["number"],
        "knowledge_status": record["knowledge_status"],
        "source_hash": record["sha256"],
        "checks": results,
        "counts": dict(Counter(item["status"] for item in results)),
        "annotation_status": suite.annotation_status,
        "quality_metrics": None,
        "note": "固定来源上的局部结构与原文核对。未命中需复核；命中不证明语义完整、政策现行有效或人工标注完成。",
    }


def evaluate_answer(answer, probe, records, suite):
    """引用不只核对文字，还要核对当前发布状态、固定来源、空间和地区。"""
    citations = answer.get("citations", [])
    checks = {
        "status": answer.get("status") in probe.allowed_statuses,
        "answer_fragments": all(
            compact(v) in compact(answer.get("answer", "")) for v in probe.required_answer_fragments
        ),
        "citation_fragments": all(
            any(compact(v) in compact(c.get("quote", "")) for c in citations)
            for v in probe.required_citation_fragments
        ),
        "citation_count": not citations
        if probe.no_citations
        else (bool(citations) if probe.required_citation_fragments else True),
        "model": bool(answer.get("generation", {}).get("used")) if probe.require_model else True,
        "source_integrity": True,
    }
    for citation in citations:
        record = records.get(citation.get("revision_id"))
        chunk = next(
            (c for c in (record or {}).get("chunks", []) if c["id"] == citation.get("chunk_id")), None
        )
        left, right, quote = citation.get("start"), citation.get("end"), citation.get("quote")
        checks["source_integrity"] &= bool(
            record
            and record["knowledge_status"] == "PUBLISHED"
            and record["sha256"] == suite.source_sha256
            and record["metadata"].get("region") == suite.region
            and not record["metadata"].get("synthetic")
            and chunk
            and type(left) is int
            and type(right) is int
            and 0 <= left < right <= len(chunk["text"])
            and chunk["text"][left:right] == quote
        )
    return {
        "id": probe.id,
        "label": probe.label,
        "passed": all(checks.values()),
        "checks": checks,
        "note": "文本片段及引用契约检查，不能替代断言级语义支持性人工评估。",
    }
