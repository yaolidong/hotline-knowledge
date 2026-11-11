"""Live 金标准：需 HOTLINE_RUN_GOLD=1，对已发布知识跑问答并写报告。不编造指标。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hotline.app import create_app
from hotline.gold import evaluate_qa_pack, iter_gold_packs, write_report
from hotline.models import Question
from hotline.settings import Settings


def main() -> int:
    if os.environ.get("HOTLINE_RUN_GOLD") != "1":
        print(json.dumps({"status": "skipped", "reason": "未设置 HOTLINE_RUN_GOLD=1"}, ensure_ascii=False))
        return 0
    settings = Settings()
    app = create_app(settings)
    query = app.state.query
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = ROOT / "test-results" / "gold" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = settings.llm_model or "unknown-model"
    reports = []
    for path, gold in iter_gold_packs(ROOT / "examples" / "gold", synthetic_only=True):
        answers = []
        for item in gold.get("questions") or []:
            request = Question(
                question=item["question"],
                region=gold["document"].get("region") or None,
                space="test",
            )
            try:
                result = query.ask(request)
            except Exception as exc:  # noqa: BLE001 — 评测需保留失败样本
                result = {"status": "ERROR", "answer": str(exc), "facts": [], "citations": []}
            answers.append(
                {
                    "question_id": item["id"],
                    "status": result.get("status"),
                    "answer": result.get("answer", ""),
                    "facts": result.get("facts") or [],
                    "citations": result.get("citations") or [],
                }
            )
        report = evaluate_qa_pack(answers, gold)
        report["model"] = backend
        report["gold_path"] = str(path)
        reports.append(report)
        write_report(out_dir / f"{gold['pack_id']}-qa.json", report)
        backlog = ROOT / "data" / "gold" / "backlog"
        backlog.mkdir(parents=True, exist_ok=True)
        for detail in report.get("details") or []:
            if not detail.get("ok"):
                fail = {
                    "pack_id": gold["pack_id"],
                    "question_id": detail["id"],
                    "status": detail.get("status"),
                    "phase_hint": "phase-2-or-3",
                    "created": stamp,
                }
                (backlog / f"{gold['pack_id']}-{detail['id']}.json").write_text(
                    json.dumps(fail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
    summary = {"backend": backend, "packs": len(reports), "output": str(out_dir), "reports": reports}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(out_dir), "packs": len(reports)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
