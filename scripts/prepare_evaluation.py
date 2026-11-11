"""从真实已解析版本准备人工标注包；不将模型输出填成标准答案。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hotline.gold import GOLD_SCHEMA_VERSION, SPLIT_HOLD, SPLIT_TRAIN
from hotline.settings import Settings
from hotline.store import Store


def build_case(record: dict, *, split: str) -> dict:
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "pack_id": record["id"],
        "split": split,
        "synthetic": bool(record["metadata"].get("synthetic")),
        "annotation_status": "PENDING_HUMAN_REVIEW",
        "annotator": "",
        "reviewer": "",
        "document": {
            "title": record["metadata"].get("title") or record["filename"],
            "region": record["metadata"].get("region", ""),
            "filename": record["filename"],
            "sha256": record["sha256"],
            "document_type": record["metadata"].get("document_type", ""),
            "revision_id": record["id"],
            "revision_number": record["number"],
        },
        "source_regions": (record["parsed_metadata"] or {}).get("_source", {}),
        "chunks": record["chunks"],
        "expected_entities": [],
        "expected_relations": [],
        "expected_table_cells": [],
        "questions": [],
        "notes": "填写完整标注并人工复核后才能统计质量；模型抽取结果没有预填为真值。将 annotation_status 改为 HUMAN_REVIEWED。",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision_id", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=[SPLIT_TRAIN, SPLIT_HOLD], default=SPLIT_TRAIN)
    args = parser.parse_args(argv)
    store = Store(Settings().data_dir)
    cases = []
    for rid in args.revision_id:
        record = store.get_revision(rid)
        if record["task_status"] != "READY":
            raise SystemExit("只有已解析完成的版本能准备标注包")
        cases.append(build_case(record, split=args.split))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": GOLD_SCHEMA_VERSION,
        "cases": cases,
        "metrics": None,
        "note": "批量待标注；单包请拆成 pack.json 并复核后再计分",
    }
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(f"已创建 {len(cases)} 份待人工标注资料；尚无质量指标。")


if __name__ == "__main__":
    main()
