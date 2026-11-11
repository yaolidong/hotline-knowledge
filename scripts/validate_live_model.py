"""通过真实本地 API 导入合成文件并记录实际模型结果，不提供替身或内置抽取答案。"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--file", type=Path, default=Path("examples/synthetic_guide.txt"))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--reprocess", help="重新处理指定版本，不重复创建文档")
    args = parser.parse_args()
    settings = Settings()
    token = next(key for key, user in settings.users.items() if user.role == "admin")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = settings.data_dir / "validation" / run_id
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "run_id": run_id,
        "provider": "DeepSeek",
        "model": settings.llm_model,
        "synthetic": True,
        "source_file": args.file.name,
        "stage": "import",
    }
    with httpx.Client(
        base_url=args.base_url, headers={"Authorization": "Bearer " + token}, timeout=30, trust_env=False
    ) as client:
        if args.reprocess:
            response = client.post(f"/api/revisions/{args.reprocess}/reprocess")
            response.raise_for_status()
            rid = response.json()["revision_id"]
        else:
            response = client.post(
                "/api/import/files",
                files={"files": (args.file.name, args.file.read_bytes())},
                data={
                    "metadata": json.dumps(
                        {
                            "title": "DeepSeek真实验证·合成办事指南",
                            "region": "测试甲区",
                            "document_type": "guide",
                            "synthetic": True,
                        },
                        ensure_ascii=False,
                    )
                },
            )
            response.raise_for_status()
            rid = response.json()["items"][0]["revision_id"]
        report["revision_id"] = rid
        print(json.dumps({"revision_id": rid, "report_dir": str(output)}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        prior = None
        while time.monotonic() - started < args.timeout:
            response = client.get(f"/api/revisions/{rid}")
            response.raise_for_status()
            record = response.json()
            state = (record["task_status"], record["progress"], record["total"])
            if state != prior:
                print(
                    json.dumps(
                        {"task_status": state[0], "progress": state[1], "total": state[2]}, ensure_ascii=False
                    ),
                    flush=True,
                )
                prior = state
            if record["task_status"] in ("READY", "FAILED"):
                break
            time.sleep(2)
        report.update(
            {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "task_status": record["task_status"],
                "knowledge_status": record["knowledge_status"],
                "error": record["error"],
                "chunks": len(record.get("chunks") or []),
            }
        )
        if record.get("extraction"):
            report["entities"] = len(record["extraction"]["entities"])
            report["relations"] = len(record["extraction"]["relations"])
            (output / "extraction.json").write_text(
                json.dumps(record["extraction"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (output / "revision.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["dependencies"] = client.get("/api/status").json()
        report["stage"] = (
            "extraction_complete" if record["task_status"] == "READY" else "extraction_incomplete"
        )
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if record["task_status"] != "READY":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
