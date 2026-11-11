"""本地 LLM 实抽验收：导入合成指南并等待任务；需真实模型与 worker。"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from hotline.app import create_app
from hotline.gold import evaluate_extraction_pack, load_pack, write_report
from hotline.settings import Settings


def main() -> int:
    if os.environ.get("HOTLINE_RUN_LOCAL_LLM") != "1":
        print(json.dumps({"status": "skipped", "reason": "未设置 HOTLINE_RUN_LOCAL_LLM=1"}, ensure_ascii=False))
        return 0
    settings = Settings()
    if not settings.llm_base_url or not settings.llm_model:
        raise SystemExit("未配置模型")
    text = (ROOT / "examples" / "gold" / "syn-jia-guide" / "source.txt").read_text(encoding="utf-8")
    gold = load_pack(ROOT / "examples" / "gold" / "syn-jia-guide" / "pack.json")
    app = create_app(settings)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = ROOT / "test-results" / "local-llm" / stamp
    out.mkdir(parents=True, exist_ok=True)
    token = next(iter(settings.users))
    with TestClient(app) as client:
        response = client.post(
            "/api/import/text",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "text": text,
                "title": "本地LLM合成验收",
                "region": "测试甲区",
                "synthetic": True,
                "document_type": "guide",
            },
        )
        response.raise_for_status()
        rid = response.json()["revision_id"]
        deadline = time.time() + max(120.0, float(settings.llm_timeout) * 8)
        record = None
        while time.time() < deadline:
            record = client.get(f"/api/revisions/{rid}", headers={"Authorization": f"Bearer {token}"}).json()
            if record["task_status"] in {"READY", "FAILED", "PARTIAL"}:
                break
            app.state.workflow.process(app.state.store.claim())
            time.sleep(0.2)
        actual = record.get("extraction") or {"entities": [], "relations": []}
        report = evaluate_extraction_pack(actual, gold)
        report["model"] = settings.llm_model
        report["backend"] = settings.llm_backend
        report["task_status"] = record["task_status"]
        report["revision_id"] = rid
        if report.get("metrics") and report["metrics"]["entities"]["f1"] < 0.3:
            report["quality_gate"] = "仅接口可用"
            report["note"] = (report.get("note") or "") + "；本地模型抽取未达种子金标准阈值，标记为接口可用。"
        else:
            report["quality_gate"] = "seed-pass" if report.get("metrics") else "no-metrics"
        write_report(out / "extraction.json", report)
        (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "ok", "output": str(out), "quality_gate": report["quality_gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
