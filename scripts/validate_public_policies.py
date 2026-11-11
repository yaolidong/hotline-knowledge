"""导入政府公开资料并执行真实模型抽取；始终保留待审核状态，不自动发布。"""

import json
import time
from pathlib import Path

import httpx

from hotline.settings import Settings


def main():
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest"), "仅用于独立测试项目"
    token = next(key for key, user in settings.users.items() if user.role == "admin")
    root = Path("data/public-policies")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    reports = []
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": "Bearer " + token},
        timeout=40,
        trust_env=False,
    ) as client:
        for source in manifest:
            if source["parse_status"] not in ("PARSED", "DERIVED_TEXT"):
                continue
            path = (
                Path(source["local_text"])
                if source["parse_status"] == "DERIVED_TEXT"
                else root / source["filename"]
            )
            metadata = {key: source[key] for key in ("title", "region", "document_type")}
            if source["parse_status"] == "DERIVED_TEXT":
                metadata["title"] += "（官网正文提取验证）"
            metadata.update(source=source["source_page"], synthetic=False)
            response = client.post(
                "/api/import/files",
                files={"files": (path.name, path.read_bytes())},
                data={"metadata": json.dumps(metadata, ensure_ascii=False)},
            )
            response.raise_for_status()
            rid = response.json()["items"][0]["revision_id"]
            started = time.monotonic()
            print(json.dumps({"id": source["id"], "revision_id": rid}, ensure_ascii=False), flush=True)
            previous = None
            for _ in range(450):
                record_response = client.get(f"/api/revisions/{rid}")
                record_response.raise_for_status()
                record = record_response.json()
                state = (record["task_status"], record["progress"], record["total"])
                if state != previous:
                    print(state, flush=True)
                    previous = state
                if record["task_status"] in ("READY", "FAILED"):
                    break
                time.sleep(2)
            extraction = record.get("extraction") or {}
            report = {
                "id": source["id"],
                "revision_id": rid,
                "task_status": record["task_status"],
                "knowledge_status": record["knowledge_status"],
                "error": record["error"],
                "entities": len(extraction.get("entities", [])),
                "relations": len(extraction.get("relations", [])),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "input_format": path.suffix,
                "original_pdf_downloaded": source.get(
                    "original_pdf_downloaded", source["parse_status"] == "PARSED"
                ),
            }
            (root / (rid + ".revision.json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            reports.append(report)
            (root / (rid + ".report.json")).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (root / "extraction-report.json").write_text(
                json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
