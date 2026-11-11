import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:32]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportMetadata(StrictModel):
    title: str = Field("", max_length=300)
    region: str = Field("", max_length=100)
    document_number: str = Field("", max_length=150)
    source: str = Field("", max_length=1000)
    document_type: Literal["guide", "policy", "notice"] = "guide"
    synthetic: bool = False
    ocr_mode: Literal["auto", "force", "off"] = "auto"
    directory_mapping: dict[str, str] | None = None


class TextImport(ImportMetadata):
    text: str = Field(min_length=1, max_length=2000000)


class Chunk(StrictModel):
    id: str
    text: str
    start: int
    end: int
    page: int | None = None
    section: str = ""
    previous: str | None = None
    following: str | None = None


class Evidence(StrictModel):
    chunk_id: str
    quote: str = Field(min_length=1, max_length=12000)
    start: int | None = Field(None, ge=0)
    end: int | None = Field(None, ge=0)


class Entity(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    type: str
    name: str = Field(min_length=1, max_length=1000)
    canonical_name: str = Field("", max_length=1000)
    display_name: str = Field("", max_length=120)
    sequence_basis: str = Field("", max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    attributes: dict[str, str] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(min_length=1, max_length=30)
    rule: dict | None = None


class Relation(StrictModel):
    source: str
    target: str
    type: str
    evidence: list[Evidence] = Field(min_length=1, max_length=30)


class Extraction(StrictModel):
    schema_version: Literal[1, 2] = 1
    normalizations: list[dict] = Field(default_factory=list, max_length=3000)
    entities: list[Entity] = Field(default_factory=list, max_length=3000)
    relations: list[Relation] = Field(default_factory=list, max_length=5000)


class IssueResolution(StrictModel):
    status: Literal["verified", "false_positive", "unresolved"]
    note: str = Field(min_length=1, max_length=500)


class Review(StrictModel):
    action: Literal["approve", "reject", "publish", "retire"]
    expected_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    acknowledged_issues: list[str] = Field(default_factory=list, max_length=300)
    issue_resolutions: dict[str, IssueResolution] = Field(default_factory=dict)


class Correction(StrictModel):
    expected_version: int = Field(ge=1)
    extraction: Extraction
    note: str = Field(min_length=1, max_length=1000)


class RetryExtraction(StrictModel):
    expected_version: int = Field(ge=1)


class Question(StrictModel):
    question: str = Field(min_length=2, max_length=1000)
    region: str = Field("", max_length=100)
    service: str = Field("", max_length=300)
    service_code: str = Field("", max_length=100)
    as_of: str = ""
    space: Literal["production", "test"] | None = None
