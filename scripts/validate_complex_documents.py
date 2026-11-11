"""真实复杂原件 → 现有 HTTP 构建链 → 未发布评测图写入与回读。

原件导入后保持 DRAFT。评测图使用独立记录和文本块 ID，不进入客服发布白名单。
没有人工完整标注，因此只报告工程校验、规模和质量提示，不输出准确率。
"""

import argparse
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hotline.graph import GraphStore, build_subgraph
from hotline.models import Chunk, Extraction, stable_id
from hotline.schema import DomainSchema
from hotline.semantics import quality_report
from hotline.settings import Settings
from hotline.store import Store


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def isolated_snapshot(record, run_id):
    snapshot = copy.deepcopy(record)
    snapshot["id"] = stable_id("unpublished-document-benchmark", run_id, record["id"])
    chunk_ids = {c["id"]: stable_id(snapshot["id"], c["id"]) for c in snapshot["chunks"]}
    for chunk in snapshot["chunks"]:
        chunk["id"] = chunk_ids[chunk["id"]]
        for field in ("previous", "following"):
            chunk[field] = chunk_ids.get(chunk.get(field))
    for item in [*snapshot["extraction"]["entities"], *snapshot["extraction"]["relations"]]:
        for evidence in item["evidence"]:
            evidence["chunk_id"] = chunk_ids[evidence["chunk_id"]]
    snapshot["metadata"]["evaluation"] = {
        "source_revision": record["id"],
        "run_id": run_id,
        "status": "UNREVIEWED_EVALUATION_ONLY",
    }
    return snapshot


def inspect_record(record, source, graph, store, output, run_id, write_graph):
    result = {
        "id": source["id"],
        "revision_id": record["id"],
        "task_status": record["task_status"],
        "knowledge_status": record["knowledge_status"],
        "error": record["error"],
        "sha256": record["sha256"],
        "model": record.get("model"),
        "chunks": len(record.get("chunks") or []),
        "text_chars": len(record.get("text") or ""),
        "graph_status": "NOT_ATTEMPTED",
        "quality_metrics": None,
    }
    save(output / f"{source['id']}.revision.json", record)
    pages = (record.get("parsed_metadata") or {}).get("_source", {}).get("pages", [])
    result["pages"] = [
        {
            "page": p["page"],
            "method": p["method"],
            "regions": len(p.get("lines", [])),
            "cells": len(p.get("cells", [])),
            "tables": len(p.get("tables", [])),
            "warnings": p.get("warnings", []),
        }
        for p in pages
    ]
    outcomes = (record.get("parsed_metadata") or {}).get("_extraction", {})
    result["chunk_outcomes"] = {
        "completed": sum(p["status"] == "READY" for p in outcomes.values()),
        "failed": sum(p["status"] == "FAILED" for p in outcomes.values()),
    }
    if not record.get("extraction"):
        return result
    assert record["sha256"] == source["sha256"], "输入原件哈希不符"
    assert record["knowledge_status"] == "DRAFT", "评测不会处理已发布或已审核版本"
    chunks = [Chunk.model_validate(c) for c in record["chunks"]]
    extraction = DomainSchema().validate(Extraction.model_validate(record["extraction"]), chunks)
    chunk_map = {c.id: c for c in chunks}
    evidence_count = 0
    for item in [*extraction.entities, *extraction.relations]:
        for evidence in item.evidence:
            chunk = chunk_map[evidence.chunk_id]
            assert evidence.start is not None and evidence.end is not None
            assert chunk.text[evidence.start : evidence.end] == evidence.quote
            assert record["text"][chunk.start : chunk.end] == chunk.text
            evidence_count += 1
    result.update(
        entities=len(extraction.entities),
        relations=len(extraction.relations),
        evidence_checks=evidence_count,
        quality=quality_report(extraction),
    )
    if write_graph and record["task_status"] == "READY":
        snapshot = isolated_snapshot(record, run_id)
        expected = build_subgraph(snapshot, graph.settings.namespace)
        # 保留独立快照与审计；不改变正式版本的审核或发布状态。
        save(output / f"{source['id']}.graph-snapshot.json", snapshot)
        with store.connect() as db:
            store.audit(
                db,
                record["id"],
                "document-benchmark",
                "EVALUATION_GRAPH_WRITE",
                "未审核评测图；不进入客服发布范围",
                {"graph_record_id": snapshot["id"], "run_id": run_id},
            )
        graph.publish(snapshot)
        # publish 内部逐个比较真实节点、边 ID 与 contentHash；再使用日常图浏览接口回读。
        actual = graph.browse([snapshot["id"]], limit=150)
        assert actual["nodes"] and actual["source"] == "openspg-neo4j"
        save(output / f"{source['id']}.graph.json", actual)
        result.update(
            graph_status="VERIFIED_UNPUBLISHED",
            graph_record_id=snapshot["id"],
            graph_nodes=len(expected["resultNodes"]),
            graph_edges=len(expected["resultEdges"]),
            browse_nodes=len(actual["nodes"]),
            browse_edges=len(actual["edges"]),
            browse_truncated=actual["has_more"],
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sources", type=Path, default=Path("examples/evaluation/complex_document_sources.json")
    )
    parser.add_argument("--input", type=Path, default=Path("data/complex-documents"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument(
        "--reprocess", action="store_true", help="显式为清单中已存在的资料新建处理版本，保留历史"
    )
    parser.add_argument("--write-evaluation-graph", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or Path("test-results/complex-documents") / run_id
    output.mkdir(parents=True, exist_ok=False)
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    sources = [s for s in sources if not args.ids or s["id"] in args.ids]
    settings = Settings()
    assert settings.namespace.startswith("HotlineTest"), "必须使用现有独立测试项目"
    token = next(k for k, u in settings.users.items() if u.role == "admin")
    graph, store = GraphStore(settings), Store(settings.data_dir)
    report = {
        "run_id": run_id,
        "input": str(args.input),
        "mode": "REAL_HTTP_KAG_OPENSPG_NEO4J",
        "publication": "DRAFT_ONLY",
        "quality_metrics": None,
        "items": [],
    }
    with httpx.Client(
        base_url="http://127.0.0.1:8080",
        timeout=60,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        pending, states = {}, {}
        for source in sources:
            data = (args.input / source["filename"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == source["sha256"], source["id"]
            metadata = {k: source[k] for k in ("title", "region", "document_type")}
            metadata.update(source=source["source_page"], synthetic=False)
            response = client.post(
                "/api/import/files",
                files={"files": (source["filename"], data)},
                data={"metadata": json.dumps(metadata, ensure_ascii=False)},
            )
            response.raise_for_status()
            item = response.json()["items"][0]
            if args.reprocess and item["duplicate"]:
                response = client.post(f"/api/revisions/{item['revision_id']}/reprocess")
                response.raise_for_status()
                item = response.json()
            pending[item["revision_id"]] = source
            print(json.dumps({"source": source["id"], **item}), flush=True)
        save(output / "imports.json", pending)
        started = time.monotonic()
        while pending and time.monotonic() - started < args.timeout:
            for rid, source in list(pending.items()):
                response = client.get(f"/api/revisions/{rid}")
                response.raise_for_status()
                record = response.json()
                state = (record["task_status"], record["progress"], record["total"])
                if states.get(rid) != state:
                    print(json.dumps({"source": source["id"], "state": state}), flush=True)
                    states[rid] = state
                if record["task_status"] not in ("READY", "PARTIAL", "FAILED"):
                    continue
                try:
                    result = inspect_record(
                        record, source, graph, store, output, run_id, args.write_evaluation_graph
                    )
                    after = client.get(f"/api/revisions/{rid}").json()
                    assert after["version"] == record["version"]
                    assert after["knowledge_status"] == "DRAFT"
                    # 客服图接口不能获取独立评测记录，亦不能读取未发布原件版本。
                    visible = store.visible_ids(published_only=True)
                    assert rid not in visible and result.get("graph_record_id") not in visible
                    assert not graph.browse(
                        visible, focus=stable_id(result.get("graph_record_id", rid), "document")
                    )["nodes"]
                    result["published_scope_check"] = "NOT_VISIBLE"
                except Exception as error:
                    result = {"id": source["id"], "revision_id": rid, "validation_error": str(error)}
                report["items"].append(result)
                save(output / "report.json", report)
                print(json.dumps(result, ensure_ascii=True), flush=True)
                del pending[rid]
            if pending:
                time.sleep(3)
        report["pending"] = pending
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        save(output / "report.json", report)
    graph.close()
    print(str(output), flush=True)
    if pending or any(i.get("validation_error") or i.get("task_status") != "READY" for i in report["items"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
