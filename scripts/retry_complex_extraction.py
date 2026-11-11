"""真实 HTTP 重试失败块，核验成功块未再次调用模型，仍不发布知识。"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hotline.graph import GraphStore
from hotline.settings import Settings
from hotline.store import Store
from scripts.validate_complex_documents import inspect_record, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision_ids", nargs="+")
    parser.add_argument("--write-evaluation-graph", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path("test-results/extraction-recovery") / ("retry-" + run_id)
    output.mkdir(parents=True, exist_ok=False)
    sources = json.loads(
        Path("examples/evaluation/complex_document_sources.json").read_text(encoding="utf-8")
    )
    token = next(k for k, user in settings.users.items() if user.role == "admin")
    graph, store, report = GraphStore(settings), Store(settings.data_dir), []
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        trust_env=False,
        timeout=60,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        pending = {}
        for rid in args.revision_ids:
            response = client.get(f"/api/revisions/{rid}")
            response.raise_for_status()
            before = response.json()
            assert before["knowledge_status"] == "DRAFT" and before["task_status"] in ("PARTIAL", "FAILED")
            save(output / f"{rid}.before.json", before)
            response = client.post(
                f"/api/revisions/{rid}/retry-extraction", json={"expected_version": before["version"]}
            )
            response.raise_for_status()
            pending[rid] = before
        deadline = time.monotonic() + 1800
        states = {}
        while pending and time.monotonic() < deadline:
            for rid, before in list(pending.items()):
                response = client.get(f"/api/revisions/{rid}")
                response.raise_for_status()
                record = response.json()
                state = (record["task_status"], record["progress"])
                if state != states.get(rid):
                    print(json.dumps({"id": rid, "state": state}), flush=True)
                    states[rid] = state
                if record["task_status"] not in ("READY", "PARTIAL", "FAILED"):
                    continue
                old = before["parsed_metadata"]["_extraction"]
                current = record["parsed_metadata"]["_extraction"]
                reused = 0
                for cid, outcome in old.items():
                    if outcome["status"] == "READY":
                        assert current[cid] == outcome, "成功块发生重新调用或被覆盖"
                        reused += 1
                source = next(s for s in sources if s["sha256"] == record["sha256"])
                result = inspect_record(
                    record, source, graph, store, output, run_id, args.write_evaluation_graph
                )
                result["reused_successful_chunks"] = reused
                result["failure_details"] = [
                    {"chunk_id": cid, "error": o["error"]}
                    for cid, o in current.items()
                    if o["status"] == "FAILED"
                ]
                assert record["id"] not in store.visible_ids(published_only=True)
                assert record["knowledge_status"] == "DRAFT"
                result["published_scope_check"] = "NOT_VISIBLE"
                report.append(result)
                save(output / "report.json", {"items": report})
                print(
                    json.dumps(
                        {
                            k: result.get(k)
                            for k in (
                                "id",
                                "task_status",
                                "entities",
                                "relations",
                                "reused_successful_chunks",
                                "graph_status",
                            )
                        }
                    ),
                    flush=True,
                )
                del pending[rid]
            if pending:
                time.sleep(3)
        assert not pending, "重试仍未结束"
    graph.close()
    print(str(output), flush=True)
    if any(r["task_status"] != "READY" for r in report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
