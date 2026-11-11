from hotline.document_evidence import evidence_chain, material_condition_coverage
from hotline.parsers import mineru_adapter


def test_evidence_chain_text_only():
    record = {
        "id": "r1",
        "document_id": "d1",
        "sha256": "abc",
        "filename": "a.txt",
        "number": 1,
        "parsed_metadata": {},
    }
    chunk = {"id": "c1", "text": "申请材料：测试申请表", "page": 1, "section": "二", "start": 0, "end": 11}
    chain = evidence_chain(record, chunk, 5, len(chunk["text"]), assertion={"type": "RequiredMaterial"})
    assert chain["status"] == "TEXT_ONLY"
    assert chain["span"]["quote"] == chunk["text"][5:]
    assert chain["assertion"]["type"] == "RequiredMaterial"


def test_material_coverage():
    gold = {
        "expected_entities": [
            {"type": "RequiredMaterial", "name": "测试申请表"},
            {"type": "EligibilityCondition", "name": "年满十八周岁"},
        ]
    }
    actual = {"entities": [{"type": "RequiredMaterial", "name": "测试申请表"}]}
    cov = material_condition_coverage(gold, actual)
    assert cov["material_coverage"] == 1.0
    assert cov["condition_coverage"] == 0.0


def test_mineru_available_false_by_default():
    assert mineru_adapter.available() is False
