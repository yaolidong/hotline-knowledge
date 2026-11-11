"""统一文档证据对象：原件 → 页 → 区域/单元格 → chunk → 断言。不另起存储引擎。"""

from __future__ import annotations

from typing import Any

from hotline.evidence import locate_evidence


def evidence_chain(record: dict, chunk: dict, start: int, end: int, *, assertion: dict | None = None) -> dict[str, Any]:
    locator = locate_evidence(record, chunk, start, end)
    pages = []
    for region in locator.get("regions") or []:
        pages.append(
            {
                "page": region.get("page"),
                "kind": region.get("kind"),
                "box": {k: region.get(k) for k in ("x0", "y0", "x1", "y1") if k in region},
                "method": region.get("method"),
            }
        )
    return {
        "source": {
            "document_id": record["document_id"],
            "revision_id": record["id"],
            "sha256": record["sha256"],
            "filename": record.get("filename"),
        },
        "chunk": {
            "id": chunk["id"],
            "page": chunk.get("page"),
            "section": chunk.get("section"),
            "start": chunk.get("start"),
            "end": chunk.get("end"),
        },
        "span": {"start": start, "end": end, "quote": locator["quote"]},
        "regions": pages,
        "assertion": assertion or {},
        "locator": locator,
        "status": locator["status"],
    }


def material_condition_coverage(gold_pack: dict, actual_extraction: dict) -> dict[str, Any]:
    """金标准材料/条件名称命中率，用于复杂文档回归。"""
    expected_materials = {
        e["name"] if isinstance(e, dict) else e[1]
        for e in gold_pack.get("expected_entities") or []
        if (e.get("type") if isinstance(e, dict) else e[0]) == "RequiredMaterial"
    }
    expected_conditions = {
        e["name"] if isinstance(e, dict) else e[1]
        for e in gold_pack.get("expected_entities") or []
        if (e.get("type") if isinstance(e, dict) else e[0]) == "EligibilityCondition"
    }
    predicted = {e["name"] for e in actual_extraction.get("entities") or []}
    def rate(expected):
        if not expected:
            return None
        hit = sum(1 for name in expected if any(name in p or p in name for p in predicted))
        return hit / len(expected)

    return {
        "material_coverage": rate(expected_materials),
        "condition_coverage": rate(expected_conditions),
        "expected_materials": sorted(expected_materials),
        "expected_conditions": sorted(expected_conditions),
    }
