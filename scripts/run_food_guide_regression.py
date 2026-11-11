"""对既有食品指南运行来源绑定的局部核对；不导入、修改、审核或发布知识。"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hotline.evaluation import RegressionSuite, evaluate_answer, evaluate_facts
from hotline.settings import Settings


DEFAULT_REVISIONS = ["70e2524c3b4f4abdbeb589939ad52593", "c443bc49df3748c9ba696c35d1488d1f"]


def snapshot(record):
    return {key: record[key] for key in ("version", "knowledge_status", "extraction", "sha256")}


def write_report(folder, suite, report):
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 食品经营许可历史资料工程回归",
        "",
        "这是一份来源固定的局部核对清单，尚待业务人员确认；不是完整人工金标准，不计算准确率或 F1。",
        "",
        "| 版本 | 知识状态 | 核对通过 | 需复核 |",
        "|---|---|---:|---:|",
    ]
    for item in report["extractions"]:
        lines.append(
            f"| {item['version']} / {item['revision_id']} | {item['knowledge_status']} | {item['counts'].get('PASSED', 0)} | {item['counts'].get('NEEDS_REVIEW', 0)} |"
        )
    for item in report["extractions"]:
        lines.extend(["", f"## 版本 {item['version']} 的待复核项", ""])
        for check in item["checks"]:
            if check["status"] == "NEEDS_REVIEW":
                lines.append(f"- {check['label']}（{check['id']}）；依据：{check['anchor']}")
    lines.extend(
        [
            "",
            "## 来源及人工核对任务",
            "",
            *[f"- {task}" for task in suite.human_review_tasks],
            "",
            "## 实际接口回归",
            "",
            "以下检查词片段、状态和引用契约，不等于业务语义已经确认。",
            "",
        ]
    )
    for result in report["queries"]:
        failed = [key for key, passed in result.get("checks", {}).items() if not passed]
        lines.append(
            f"- {result['label']}：{'通过' if result.get('passed') else '需复核'}"
            + (f"；{', '.join(failed)}" if failed else "")
        )
    lines.extend(
        [
            "",
            f"原版本保护：{report.get('source_versions_unchanged', '待检查')}。",
            "",
            "表格单元格和扫描识别评测：未执行；本轮输入为派生 TXT，缺少原始 PDF 版面真值。",
        ]
    )
    (folder / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=Path("examples/evaluation/food_guide_regression.json"))
    parser.add_argument("--revisions", nargs="+", default=DEFAULT_REVISIONS)
    parser.add_argument("--with-qa", action="store_true", help="实际调用已配置模型；不传则仅图查询及本地核对")
    parser.add_argument("--output", type=Path, default=Path("test-results/food-guide-regression"))
    args = parser.parse_args()
    suite = RegressionSuite.model_validate_json(args.suite.read_text(encoding="utf-8"))
    settings = Settings()
    tokens = {user.role: token for token, user in settings.users.items()}
    if "admin" not in tokens or "agent" not in tokens:
        raise SystemExit("请配置管理员与客服测试令牌")
    folder = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, exist_ok=False)
    report = {
        "source_kind": suite.source_kind,
        "annotation_status": suite.annotation_status,
        "quality_metrics": None,
        "extractions": [],
        "queries": [],
        "scope_note": "一个原始来源的两个系统版本，不是两份独立政策；不验证当前效力。",
    }
    records = {}
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        trust_env=False,
        timeout=180,
        headers={"Authorization": "Bearer " + tokens["admin"]},
    ) as client:

        def get_record(rid):
            response = client.get(f"/api/revisions/{rid}")
            response.raise_for_status()
            return response.json()

        for rid in args.revisions:
            record = get_record(rid)
            raw = client.get(f"/api/source/{rid}")
            raw.raise_for_status()
            if hashlib.sha256(raw.content).hexdigest() != record["sha256"]:
                raise SystemExit("原始输入字节与保存的来源哈希不一致，停止回归")
            records[rid] = record
            report["extractions"].append(evaluate_facts(record, suite))
        originals = {rid: snapshot(record) for rid, record in records.items()}
        for probe in suite.questions:
            if probe.mode == "qa" and not args.with_qa:
                continue
            response = client.post(
                f"/api/{probe.mode}",
                json=probe.request.model_dump(),
                headers={"Authorization": "Bearer " + tokens["agent"]},
            )
            if response.is_error:
                item = {
                    "id": probe.id,
                    "label": probe.label,
                    "passed": False,
                    "checks": {"http": False},
                    "http_status": response.status_code,
                }
            else:
                answer = response.json()
                referenced = {c["revision_id"] for c in answer.get("citations", [])}
                current_records = {rid: get_record(rid) for rid in referenced}
                item = {
                    **evaluate_answer(answer, probe, current_records, suite),
                    "mode": probe.mode,
                    "response": answer,
                }
            report["queries"].append(item)
            write_report(folder, suite, report)
            print(
                json.dumps(
                    {"case": probe.id, "passed": item["passed"], "checks": item["checks"]}, ensure_ascii=False
                ),
                flush=True,
            )
        report["source_versions_unchanged"] = all(
            snapshot(get_record(rid)) == previous for rid, previous in originals.items()
        )
    write_report(folder, suite, report)
    print(
        json.dumps(
            {
                "report": str(folder / "review.md"),
                "extractions": [
                    {"revision": r["version"], "counts": r["counts"]} for r in report["extractions"]
                ],
                "query_checks": len(report["queries"]),
                "query_failures": sum(not r["passed"] for r in report["queries"]),
                "source_versions_unchanged": report["source_versions_unchanged"],
                "quality_metrics": None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["source_versions_unchanged"] or any(not r["passed"] for r in report["queries"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
