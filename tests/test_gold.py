import json
from pathlib import Path

from hotline.gold import (
    GOLD_SCHEMA_VERSION,
    evaluate_extraction_pack,
    evaluate_qa_pack,
    iter_gold_packs,
    score_sets,
)


def test_seed_manifest_has_eight_packs_and_forty_questions():
    manifest = json.loads(Path("examples/gold/manifest.json").read_text(encoding="utf-8"))
    assert manifest["totals"]["packs"] == 8
    assert manifest["totals"]["questions"] == 40
    assert manifest["schema_version"] == GOLD_SCHEMA_VERSION


def test_all_seed_packs_are_human_reviewed_and_loadable():
    packs = list(iter_gold_packs(Path("examples/gold"), synthetic_only=True))
    assert len(packs) == 8
    for path, data in packs:
        assert data["annotation_status"] == "HUMAN_REVIEWED"
        assert data["document"]["sha256"]
        assert Path(path).parent.joinpath("source.txt").exists()


def test_extraction_scoring_perfect_and_missing():
    gold = {
        "annotation_status": "HUMAN_REVIEWED",
        "pack_id": "t",
        "expected_entities": [["ServiceItem", "示例登记"]],
        "expected_relations": [["示例登记", "HANDLED_BY", "测试服务局"]],
        "chunks": [],
    }
    actual = {
        "entities": [
            {"id": "e1", "type": "ServiceItem", "name": "示例登记"},
            {"id": "e2", "type": "GovernmentDepartment", "name": "测试服务局"},
        ],
        "relations": [{"source": "e1", "type": "HANDLED_BY", "target": "e2"}],
    }
    report = evaluate_extraction_pack(actual, gold)
    assert report["metrics"]["entities"]["recall"] == 1.0
    assert report["metrics"]["entities"]["precision"] < 1.0  # 多预测了部门实体
    assert report["metrics"]["relations"]["f1"] == 1.0
    gold_full = {
        **gold,
        "expected_entities": [["ServiceItem", "示例登记"], ["GovernmentDepartment", "测试服务局"]],
    }
    perfect = evaluate_extraction_pack(actual, gold_full)
    assert perfect["metrics"]["entities"]["f1"] == 1.0
    empty = evaluate_extraction_pack({"entities": [], "relations": []}, gold)
    assert empty["metrics"]["entities"]["recall"] == 0.0


def test_unreviewed_pack_metrics_null():
    report = evaluate_extraction_pack(
        {"entities": [], "relations": []},
        {"annotation_status": "PENDING_HUMAN_REVIEW", "pack_id": "x"},
    )
    assert report["metrics"] is None


def test_qa_refuse_and_cross_region():
    gold = {
        "annotation_status": "HUMAN_REVIEWED",
        "pack_id": "qa",
        "questions": [
            {"id": "a", "expect_refuse": True},
            {"id": "b", "must_include": ["测试服务局"], "forbidden_regions": ["测试乙区"]},
        ],
    }
    answers = [
        {"question_id": "a", "status": "NO_EVIDENCE", "answer": "没有检索到"},
        {
            "question_id": "b",
            "status": "ANSWERED",
            "answer": "办理部门：测试服务局",
            "facts": [{"value": "测试服务局"}],
            "citations": [{"quote": "x"}],
        },
    ]
    report = evaluate_qa_pack(answers, gold)
    assert report["metrics"]["accuracy"] == 1.0
    bad = evaluate_qa_pack(
        [
            answers[0],
            {"question_id": "b", "status": "ANSWERED", "answer": "请到测试乙区办理", "facts": [], "citations": []},
        ],
        gold,
    )
    assert bad["metrics"]["cross_region_violations"] == 1


def test_score_sets_empty_expected_without_pred_is_perfect():
    assert score_sets(set(), set())["f1"] == 1.0
