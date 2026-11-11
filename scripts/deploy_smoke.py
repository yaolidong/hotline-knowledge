"""部署冒烟：合成指南导入 → 抽取 → 质检确认审核 → 发布 → test 空间查询。

使用当前 `.env` 指向的真实模型与图服务；结果写入 `data/validation/deploy-smoke.json`。
不打印令牌。合成资料不得用于实际政策办理。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from hotline.app import create_app
from hotline.settings import Settings


def _token(settings: Settings, role: str) -> str:
    return next(token for token, user in settings.users.items() if user.role == role)


def main() -> int:
    settings = Settings()
    if not settings.users:
        raise SystemExit("未配置 HOTLINE_USERS")
    text = (ROOT / "examples" / "synthetic_guide.txt").read_text(encoding="utf-8")
    app = create_app(settings)
    report: dict = {"steps": [], "synthetic": True}
    admin, reviewer, agent = _token(settings, "admin"), _token(settings, "reviewer"), _token(settings, "agent")
    out = Path(settings.data_dir) / "validation" / "deploy-smoke.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    with TestClient(app) as client:
        response = client.post(
            "/api/import/text",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "text": text,
                "title": "部署手册合成冒烟",
                "region": "测试甲区",
                "synthetic": True,
                "document_type": "guide",
            },
        )
        report["steps"].append({"import": response.status_code})
        response.raise_for_status()
        rid = response.json()["revision_id"]
        report["revision_id"] = rid

        deadline = time.time() + max(300.0, float(settings.llm_timeout) * 12)
        record = None
        while time.time() < deadline:
            claim = app.state.store.claim()
            if claim:
                app.state.workflow.process(claim)
            record = client.get(f"/api/revisions/{rid}", headers={"Authorization": f"Bearer {admin}"}).json()
            if record["task_status"] in {"READY", "FAILED", "PARTIAL"}:
                break
            time.sleep(0.4)

        report["task_status"] = record["task_status"]
        report["error"] = record.get("error")
        report["entities"] = len((record.get("extraction") or {}).get("entities") or [])
        if record["task_status"] != "READY":
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        if (record.get("parsed_metadata") or {}).get("_fact_review") is not None:
            report["note"] = "该草稿走事实组审核路径，请用工作台逐组发布；本脚本不自动代审事实组。"
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2

        issues = [x["id"] for x in (record.get("quality") or {}).get("issues") or []]
        for action in ("approve", "publish"):
            version = client.get(f"/api/revisions/{rid}", headers={"Authorization": f"Bearer {admin}"}).json()[
                "version"
            ]
            body = {
                "action": action,
                "expected_version": version,
                "note": "部署手册合成冒烟：仅测试空间，不代表政策审核。",
                "acknowledged_issues": issues,
            }
            response = client.post(
                f"/api/revisions/{rid}/review",
                headers={"Authorization": f"Bearer {reviewer}"},
                json=body,
            )
            report["steps"].append(
                {"action": action, "status": response.status_code, "detail": response.text[:300]}
            )
            response.raise_for_status()

        query = client.post(
            "/api/query",
            headers={"Authorization": f"Bearer {agent}"},
            json={"question": "示例登记需要什么材料", "region": "测试甲区", "space": "test"},
        )
        report["steps"].append({"query": query.status_code})
        query.raise_for_status()
        payload = query.json()
        report["query_status"] = payload.get("status")
        report["citations"] = len(payload.get("citations") or [])
        report["ok"] = bool(report["citations"]) or payload.get("status") in {
            "ANSWERED",
            "PARTIAL",
            "NEEDS_CLARIFICATION",
        }

    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "error" or report.get("error")}, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
