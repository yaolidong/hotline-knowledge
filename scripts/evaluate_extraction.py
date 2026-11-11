"""比较实际抽取结果与人工复核金标准；无复核时指标为 null。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hotline.gold import evaluate_extraction_pack, load_pack, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("actual", type=Path, help="实际抽取 JSON（entities/relations）")
    parser.add_argument("gold", type=Path, help="人工复核 pack.json 或旧式 expected 列表文件")
    parser.add_argument("--output", type=Path, help="写入 report.json")
    args = parser.parse_args(argv)
    actual = json.loads(args.actual.read_text(encoding="utf-8"))
    raw = json.loads(args.gold.read_text(encoding="utf-8"))
    if raw.get("schema_version") == 2:
        gold = load_pack(args.gold) if raw.get("annotation_status") == "HUMAN_REVIEWED" else raw
        report = evaluate_extraction_pack(actual, gold)
    else:
        # 兼容旧 examples/expected_facts.json：仅在有完整列表时计分，并标注非全量基准
        gold = {
            "annotation_status": "HUMAN_REVIEWED",
            "pack_id": "legacy-expected-facts",
            "expected_entities": raw.get("entities") or [],
            "expected_relations": [
                {"source_name": a, "type": b, "target_name": c} for a, b, c in (raw.get("relations") or [])
            ],
            "chunks": [],
        }
        report = evaluate_extraction_pack(actual, gold)
        report["note"] = "兼容旧 expected_facts；不是全量基准，不得外推总体质量"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        write_report(args.output, report)
    return 0 if report.get("metrics") is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
