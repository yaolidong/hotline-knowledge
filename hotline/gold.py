"""金标准评测契约与打分；无人工复核真值时指标必须为 null，禁止伪造准确率。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hotline.semantics import canonical_name

GOLD_SCHEMA_VERSION = 2
SPLIT_TRAIN = "train"
SPLIT_HOLD = "holdout"


def normalize_entity_key(entity_type: str, name: str) -> tuple[str, str]:
    return entity_type, canonical_name(name)


def entity_keys_from_list(entities: list[dict]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for entity in entities:
        keys.add(normalize_entity_key(entity["type"], entity["name"]))
        for alias in entity.get("aliases") or []:
            keys.add(normalize_entity_key(entity["type"], alias))
    return keys


def relation_keys(entities: list[dict], relations: list[dict]) -> set[tuple[str, str, str]]:
    names = {e["id"]: e["name"] for e in entities if "id" in e and "name" in e}
    keys: set[tuple[str, str, str]] = set()
    for relation in relations:
        source = relation.get("source_name") or names.get(relation.get("source", ""), "")
        target = relation.get("target_name") or names.get(relation.get("target", ""), "")
        if not source or not target:
            continue
        keys.add((canonical_name(source), relation["type"], canonical_name(target)))
    return keys


def score_sets(predicted: set, expected: set) -> dict[str, Any] | None:
    if not expected and not predicted:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "true_positive": 0, "predicted": 0, "gold": 0}
    if not expected:
        return None
    positive = len(expected & predicted)
    precision = positive / len(predicted) if predicted else 0.0
    recall = positive / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positive": positive,
        "predicted": len(predicted),
        "gold": len(expected),
        "missing": sorted(expected - predicted),
        "extra": sorted(predicted - expected),
    }


def load_pack(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != GOLD_SCHEMA_VERSION:
        raise ValueError(f"金标准 schema_version 必须为 {GOLD_SCHEMA_VERSION}：{path}")
    if data.get("annotation_status") != "HUMAN_REVIEWED":
        raise ValueError(f"未人工复核的包不得用于计分：{path}")
    return data


def evaluate_extraction_pack(actual: dict, gold: dict) -> dict[str, Any]:
    if gold.get("annotation_status") != "HUMAN_REVIEWED":
        return {
            "metrics": None,
            "note": "标注未复核，禁止报告 Precision/Recall/F1",
            "pack_id": gold.get("pack_id"),
        }
    predicted_entities = entity_keys_from_list(actual.get("entities") or [])
    raw_expected = gold.get("expected_entities") or []
    if raw_expected and isinstance(raw_expected[0], list):
        expected_entities = {normalize_entity_key(t, n) for t, n in raw_expected}
    else:
        expected_entities = entity_keys_from_list(raw_expected)
    predicted_relations = relation_keys(actual.get("entities") or [], actual.get("relations") or [])
    expected_relations = set()
    for item in gold.get("expected_relations") or []:
        if isinstance(item, list):
            expected_relations.add((canonical_name(item[0]), item[1], canonical_name(item[2])))
        else:
            expected_relations.add(
                (
                    canonical_name(item["source_name"]),
                    item["type"],
                    canonical_name(item["target_name"]),
                )
            )
    evidence_ok = True
    for entity in raw_expected:
        if not isinstance(entity, dict) or not entity.get("evidence"):
            continue
        for ev in entity["evidence"]:
            chunk_id = ev.get("chunk_id")
            quote = ev.get("quote", "")
            chunk = next((c for c in gold.get("chunks") or [] if c.get("id") == chunk_id), None)
            if chunk and quote and quote not in chunk.get("text", ""):
                evidence_ok = False
    return {
        "pack_id": gold.get("pack_id"),
        "split": gold.get("split"),
        "metrics": {
            "entities": score_sets(predicted_entities, expected_entities),
            "relations": score_sets(predicted_relations, expected_relations),
            "evidence_quotes_ok": evidence_ok,
        },
        "note": "仅评估本包人工复核真值，不代表总体政策质量",
    }


def evaluate_qa_pack(actual_answers: list[dict], gold: dict) -> dict[str, Any]:
    if gold.get("annotation_status") != "HUMAN_REVIEWED":
        return {"metrics": None, "note": "标注未复核，禁止报告问答准确率", "pack_id": gold.get("pack_id")}
    questions = gold.get("questions") or []
    by_id = {a.get("question_id"): a for a in actual_answers}
    correct = 0
    grounded = 0
    refuse_ok = 0
    cross_region = 0
    total = len(questions)
    details = []
    for item in questions:
        qid = item["id"]
        got = by_id.get(qid) or {}
        status = got.get("status", "")
        answer = got.get("answer", "")
        expect_refuse = bool(item.get("expect_refuse"))
        expect_status = item.get("expect_status")
        forbidden = item.get("forbidden_regions") or []
        ok = True
        if expect_refuse:
            ok = status in {
                "NO_EVIDENCE",
                "NEEDS_CLARIFICATION",
                "VALIDITY_UNCONFIRMED",
            } or any(x in answer for x in ("没有", "未找到", "不能", "请核对", "尚待核实"))
            if ok:
                refuse_ok += 1
        elif expect_status:
            ok = status == expect_status
        else:
            must = item.get("must_include") or []
            ok = all(m in answer or any(m in f.get("value", "") for f in got.get("facts") or []) for m in must)
        if forbidden and any(region in answer for region in forbidden):
            ok = False
            cross_region += 1
        if got.get("citations") or got.get("facts"):
            grounded += 1
        if ok:
            correct += 1
        details.append({"id": qid, "ok": ok, "status": status})
    metrics = None
    if total:
        metrics = {
            "accuracy": correct / total,
            "correct_refuse_rate": refuse_ok / max(1, sum(1 for q in questions if q.get("expect_refuse"))),
            "grounded_rate": grounded / total,
            "cross_region_violations": cross_region,
            "total": total,
            "correct": correct,
        }
    return {"pack_id": gold.get("pack_id"), "metrics": metrics, "details": details}


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review = path.with_suffix(".md") if path.suffix != ".md" else path
    if path.suffix == ".json":
        review = path.with_name(path.stem + "-review.md")
    lines = ["# 金标准评测报告", "", f"- pack: {report.get('pack_id', report.get('packs'))}", ""]
    metrics = report.get("metrics")
    if metrics is None:
        lines.append("指标为 null：未复核或不计分。")
    else:
        lines.append("```json")
        lines.append(json.dumps(metrics, ensure_ascii=False, indent=2))
        lines.append("```")
    lines.append("")
    lines.append(report.get("note", ""))
    review.write_text("\n".join(lines) + "\n", encoding="utf-8")


def iter_gold_packs(root: Path, *, synthetic_only: bool = False, split: str | None = None):
    for path in sorted(root.glob("**/pack.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != GOLD_SCHEMA_VERSION:
            continue
        if synthetic_only and not data.get("synthetic", False):
            continue
        if split and data.get("split") != split:
            continue
        yield path, data


def slug(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.strip(), flags=re.UNICODE)
    return text.strip("-")[:80] or "pack"
