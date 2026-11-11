"""合成金标准冒烟：不调用模型/图服务，仅校验包完整性与离线打分契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hotline.gold import evaluate_extraction_pack, evaluate_qa_pack, iter_gold_packs


def main() -> int:
    root = ROOT / "examples" / "gold"
    packs = list(iter_gold_packs(root, synthetic_only=True))
    if len(packs) < 8:
        raise SystemExit(f"合成金标准不足 8 包：{len(packs)}")
    questions = 0
    for path, gold in packs:
        questions += len(gold.get("questions") or [])
        # 自洽：用金标准实体作为「完美预测」应得到实体 recall=1
        actual = {
            "entities": [
                e
                if isinstance(e, dict)
                else {"id": f"g{i}", "type": e[0], "name": e[1]}
                for i, e in enumerate(gold.get("expected_entities") or [])
            ],
            "relations": [],
        }
        # relations need ids — skip full relation self-score; ensure pack loads
        report = evaluate_extraction_pack(actual, gold)
        if report.get("metrics") is None:
            raise SystemExit(f"包不可计分：{path}")
        if report["metrics"]["entities"]["recall"] != 1.0:
            raise SystemExit(f"实体自洽失败：{path}")
        # QA：全部拒答型用 NO_EVIDENCE 应计 refuse
        answers = []
        for q in gold.get("questions") or []:
            if q.get("expect_refuse"):
                answers.append(
                    {"question_id": q["id"], "status": "NO_EVIDENCE", "answer": "没有检索到可用于回答的已发布知识。"}
                )
            else:
                must = q.get("must_include") or ["占位"]
                answers.append(
                    {
                        "question_id": q["id"],
                        "status": "ANSWERED",
                        "answer": "；".join(must),
                        "facts": [{"value": m} for m in must],
                        "citations": [{"quote": must[0]}],
                    }
                )
        qa = evaluate_qa_pack(answers, gold)
        if not qa.get("metrics") or qa["metrics"]["accuracy"] < 1.0:
            raise SystemExit(f"问答自洽失败：{path} -> {qa}")
    summary = {"packs": len(packs), "questions": questions, "status": "ok"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
