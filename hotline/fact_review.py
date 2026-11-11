"""事实组审核与不可变发布快照；复用版本白名单，不向问答暴露草稿全文。"""

import copy
import hashlib
import json
import uuid
from typing import Literal

from pydantic import Field

from hotline.models import Chunk, Extraction, IssueResolution, StrictModel, now, stable_id
from hotline.semantics import quality_report
from hotline.settings import ServiceError
from hotline.store import dumps


class ReviewCommand(StrictModel):
    expected_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)


class ItemDecision(ReviewCommand):
    action: Literal["accept", "return", "reject"]


class Coverage(StrictModel):
    status: Literal["included", "unrelated"]
    note: str = Field("", max_length=1000)


class GroupDecision(ReviewCommand):
    action: Literal["approve", "return"]
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    checks: dict[str, bool] = Field(default_factory=dict)
    issue_resolutions: dict[str, IssueResolution] = Field(default_factory=dict)


class ManualBlock(ReviewCommand):
    extraction: Extraction
    no_facts: bool = False


CHECKS = {
    "conditions": "条件、否定和例外完整",
    "materials": "材料备选、流程及依赖完整",
    "scope": "地区、主体及时间范围一致",
    "evidence": "已对照原件核对证据及遗漏",
}
HUBS = {"GovernmentDepartment", "AdministrativeRegion", "LegalBasis", "PolicyDocument"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def item_key(item):
    return (
        "entity:" + item["id"]
        if "id" in item
        else "relation:" + stable_id(item["source"], item["type"], item["target"])
    )


def objects(record):
    extraction = record.get("extraction") or {}
    return {item_key(i): i for kind in ("entities", "relations") for i in extraction.get(kind, [])}


def review_state(record):
    return (record.get("parsed_metadata") or {}).get("_fact_review", {})


def frozen(record):
    return bool((record.get("parsed_metadata") or {}).get("_group_publication")) or any(
        g.get("publication_revision_id") for g in review_state(record).get("groups", {}).values()
    )


def reset_review(metadata, extraction):
    """修正使组审批失效；逐项判断只在对象内容未变时沿用。"""
    state = metadata.get("_fact_review")
    if state is not None:
        items = objects({"extraction": extraction})
        state["items"] = {
            key: decision
            for key, decision in state.get("items", {}).items()
            if key in items and decision.get("fingerprint") == fingerprint(items[key])
        }
        state["groups"] = {}


def manual_baseline(record, extraction, metadata):
    """已人工修正内容作为恢复基线，后续失败块重试不能覆盖它。"""
    metadata["_manual_baseline"] = extraction
    for chunk in record["chunks"]:
        outcome = metadata.get("_extraction", {}).get(chunk["id"])
        if outcome and outcome["status"] == "READY":
            outcome.update(
                method="manual", source_hash=fingerprint(chunk), extraction=Extraction().model_dump()
            )


def fact_groups(record):
    items = objects(record)
    if not items:
        return []
    entities = {i["id"]: i for i in items.values() if "id" in i}
    parents = {key: key for key in items}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        parents[root(right)] = root(left)

    # 事项及其要求不能拆散；共享部门/地区/依据不单凭名称连成整份文档。
    for key, item in items.items():
        if "id" in item:
            continue
        for endpoint in (item["source"], item["target"]):
            if entities[endpoint]["type"] not in HUBS:
                union(key, "entity:" + endpoint)
    grouped = {}
    for key in items:
        if "id" in items[key] and items[key]["type"] in HUBS:
            continue
        grouped.setdefault(root(key), set()).add(key)
    used = set()
    for keys in grouped.values():
        for key in list(keys):
            if "id" not in items[key]:
                keys.update("entity:" + items[key][endpoint] for endpoint in ("source", "target"))
        used.update(keys)
    for key in items.keys() - used:
        grouped[key] = {key}
    candidates = list(grouped.values())

    # 同一段不可分的证据不能被两个组分别宣称审核。共享元数据节点本身不触发合组。
    def spans(keys):
        return [(e["chunk_id"], e["start"], e["end"]) for key in keys for e in items[key]["evidence"]]

    changed = True
    while changed:
        changed = False
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                left, right = candidates[i], candidates[j]
                shared = left & right
                if any(
                    a == b and start < end2 and start2 < end
                    for unique, other in ((left - shared, right), (right - shared, left))
                    for a, start, end in spans(unique)
                    for b, start2, end2 in spans(other)
                ):
                    left.update(right)
                    candidates.pop(j)
                    changed = True
                    break
            if changed:
                break
    state = review_state(record)
    result = []
    for keys in candidates:
        keys = sorted(keys)
        gid = stable_id("fact-group", *keys)
        selected = [items[k] for k in keys]
        services = [i["name"] for i in selected if i.get("type") == "ServiceItem"]
        title = "、".join(services) or next(i["name"] for i in selected if "id" in i)
        extraction = subset(record, keys)
        decision = state.get("groups", {}).get(gid, {})
        result.append(
            {
                "id": gid,
                "title": title,
                "item_keys": keys,
                "chunk_ids": sorted({e["chunk_id"] for i in selected for e in i["evidence"]}),
                "quality": quality_report(extraction),
                "status": decision.get("status", "DRAFT"),
                "decision": decision,
            }
        )
    return sorted(result, key=lambda group: (group["title"], group["id"]))


def subset(record, keys):
    selected = set(keys)
    return {
        "schema_version": (record.get("extraction") or {}).get("schema_version", 2),
        "entities": [
            i for i in (record.get("extraction") or {}).get("entities", []) if item_key(i) in selected
        ],
        "relations": [
            i for i in (record.get("extraction") or {}).get("relations", []) if item_key(i) in selected
        ],
    }


def publication_content(record, group, rid):
    """仅复制核对过的连续证据范围，并重建块 ID；原文件和绝对偏移保持可追溯。"""
    extraction = copy.deepcopy(subset(record, group["item_keys"]))
    source_chunks = {c["id"]: c for c in record["chunks"]}
    ranges = {}
    for kind in ("entities", "relations"):
        for item in extraction[kind]:
            for e in item["evidence"]:
                ranges.setdefault(e["chunk_id"], []).append((e["start"], e["end"]))
    chunks, mapping = [], {}
    for cid, spans in ranges.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            source = source_chunks[cid]
            new_id = stable_id(rid, cid, str(start), str(end))
            chunks.append(
                {
                    **source,
                    "id": new_id,
                    "text": source["text"][start:end],
                    "start": source["start"] + start,
                    "end": source["start"] + end,
                    "previous": None,
                    "following": None,
                }
            )
            mapping[new_id] = {"chunk_id": cid, "start": start, "end": end}
    for kind in ("entities", "relations"):
        for item in extraction[kind]:
            for e in item["evidence"]:
                new_id, span = next(
                    (key, value)
                    for key, value in mapping.items()
                    if value["chunk_id"] == e["chunk_id"]
                    and value["start"] <= e["start"]
                    and e["end"] <= value["end"]
                )
                e.update(chunk_id=new_id, start=e["start"] - span["start"], end=e["end"] - span["start"])
    return extraction, sorted(chunks, key=lambda c: c["start"]), mapping


class FactReviewService:
    def __init__(self, store, schema, graph):
        self.store, self.schema, self.graph = store, schema, graph

    def detail(self, record):
        state = review_state(record)
        names = {i["id"]: i["name"] for i in objects(record).values() if "id" in i}
        rows = []
        for key, item in objects(record).items():
            decision = state.get("items", {}).get(key, {})
            if decision.get("fingerprint") != fingerprint(item):
                decision = {}
            rows.append(
                {
                    "key": key,
                    "kind": "entity" if "id" in item else "relation",
                    "value": item,
                    "label": item.get("name")
                    or f"{names[item['source']]} → {item['type']} → {names[item['target']]}",
                    "status": decision.get("status", "PENDING"),
                    "decision": decision,
                }
            )
        return {
            "groups": fact_groups(record),
            "items": rows,
            "checks": CHECKS,
            "frozen": frozen(record),
            "enabled": bool(state),
        }

    def editable(self, db, rid, command, allow_frozen=False):
        record = self.store.get_revision(rid, db)
        if record["version"] != command.expected_version:
            raise ServiceError("记录已更新，请刷新后重试", 409)
        if not command.note.strip():
            raise ServiceError("请填写具体处理依据", 409)
        if record["task_status"] not in ("READY", "PARTIAL", "FAILED") or not record["chunks"]:
            raise ServiceError("解析尚未完成，不能进入事实审核", 409)
        if record["knowledge_status"] not in ("DRAFT", "REJECTED") or (frozen(record) and not allow_frozen):
            raise ServiceError("发布快照不可修改，请新建修订草稿", 409)
        if (record.get("parsed_metadata") or {}).get("_group_publication"):
            raise ServiceError("此版本是发布快照，请返回原审核草稿", 409)
        if db.execute("SELECT 1 FROM audit WHERE revision_id=? AND action='PUBLISH'", (rid,)).fetchone():
            raise ServiceError("该版本已尝试整篇发布，请新建修订草稿", 409)
        return record

    def save(self, db, record, metadata, user, action, note, snapshot):
        db.execute(
            "UPDATE revisions SET parsed_metadata=?,version=version+1,knowledge_status='DRAFT' WHERE id=?",
            (dumps(metadata), record["id"]),
        )
        self.store.audit(db, record["id"], user.name, action, note.strip(), snapshot)

    def decide_item(self, rid, key, command, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.editable(db, rid, command)
            item = objects(record).get(key)
            if item is None:
                raise ServiceError("条目不存在，请刷新", 404)
            meta = record["parsed_metadata"] or {}
            state = meta.setdefault("_fact_review", {})
            decision = {
                "status": {"accept": "ACCEPTED", "return": "RETURNED", "reject": "REJECTED"}[command.action],
                "note": command.note.strip(),
                "actor": user.name,
                "at": now(),
                "fingerprint": fingerprint(item),
            }
            state.setdefault("items", {})[key] = decision
            state["groups"] = {}
            self.save(
                db,
                record,
                meta,
                user,
                "FACT_" + command.action.upper(),
                command.note,
                {"key": key, "item": item, "decision": decision},
            )
        return self.store.get_revision(rid)

    def decide_group(self, rid, gid, command, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.editable(db, rid, command, allow_frozen=True)
            group = next((g for g in fact_groups(record) if g["id"] == gid), None)
            if not group:
                raise ServiceError("事实组已变化，请刷新", 409)
            if group["decision"].get("publication_revision_id"):
                raise ServiceError("该组已生成发布快照，请在快照中停用或新建修订草稿", 409)
            meta = record["parsed_metadata"] or {}
            state = meta.setdefault("_fact_review", {})
            if command.action == "approve":
                blocked = [
                    i
                    for i in self.detail(record)["items"]
                    if i["key"] in group["item_keys"] and i["status"] in ("RETURNED", "REJECTED")
                ]
                if blocked:
                    raise ServiceError("组内仍有退回或拒绝的条目，请逐条修正并确认", 409)
                if set(command.checks) != set(CHECKS) or not all(command.checks.values()):
                    raise ServiceError("请核对完整条件、依赖、范围和原件", 409)
                if set(command.coverage) != {c["id"] for c in record["chunks"]}:
                    raise ServiceError("请逐块核对全文，不能漏过未抽取的条款", 409)
                outcomes = meta.get("_extraction", {})
                for cid, coverage in command.coverage.items():
                    expected = "included" if cid in group["chunk_ids"] else "unrelated"
                    if coverage.status != expected or (expected == "unrelated" and not coverage.note.strip()):
                        raise ServiceError("来源覆盖与事实组不一致，非本组条款需说明排除依据", 409)
                    if expected == "included" and outcomes.get(cid, {}).get("status", "READY") != "READY":
                        raise ServiceError("本组仍依赖失败块，请先人工补录或重试", 409)
                issues = {i["id"] for i in group["quality"]["issues"]}
                if set(command.issue_resolutions) != issues or any(
                    r.status == "unresolved" or not r.note.strip() for r in command.issue_resolutions.values()
                ):
                    raise ServiceError("请逐项处理本组质检问题并填写依据", 409)
                self.schema.validate(
                    Extraction.model_validate(subset(record, group["item_keys"])),
                    [Chunk.model_validate(c) for c in record["chunks"]],
                )
            decision = {
                "status": "APPROVED" if command.action == "approve" else "RETURNED",
                "actor": user.name,
                "at": now(),
                **command.model_dump(),
                "fingerprint": fingerprint({"extraction": record["extraction"], "chunks": record["chunks"]}),
            }
            state.setdefault("groups", {})[gid] = decision
            self.save(
                db,
                record,
                meta,
                user,
                "GROUP_" + command.action.upper(),
                command.note,
                {"group": group, "decision": decision},
            )
        return self.store.get_revision(rid)

    def repair_block(self, rid, cid, command, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.editable(db, rid, command)
            meta = record["parsed_metadata"] or {}
            outcome = meta.get("_extraction", {}).get(cid)
            if not outcome or outcome["status"] != "FAILED":
                raise ServiceError("该块不是待补录的失败块", 409)
            part = command.extraction.model_dump()
            current = Extraction.model_validate(record["extraction"] or {}).model_dump()
            if command.no_facts != (not part["entities"] and not part["relations"]):
                raise ServiceError("空结果须明确确认本块无可抽取事实；有事实时不能标为空", 409)
            known = objects(record)
            for kind in ("entities", "relations"):
                for item in part[kind]:
                    if item_key(item) in known:
                        raise ServiceError("补录只添加遗漏条目；修改已有条目请使用逐条修正", 409)
                    if not any(e["chunk_id"] == cid for e in item["evidence"]):
                        raise ServiceError("补录条目必须包含此失败块的原文证据", 409)
                current[kind].extend(part[kind])
            current["schema_version"] = 2
            validated = self.schema.validate(
                Extraction.model_validate(current), [Chunk.model_validate(c) for c in record["chunks"]]
            ).model_dump()
            before = copy.deepcopy(outcome)
            outcome.update(
                status="READY", error=None, method="manual", note=command.note.strip(), actor=user.name
            )
            manual_baseline(record, validated, meta)
            meta.setdefault("_fact_review", {})
            reset_review(meta, validated)
            pending = any(o["status"] != "READY" for o in meta["_extraction"].values())
            status = ("PARTIAL" if validated["entities"] else "FAILED") if pending else "READY"
            db.execute(
                "UPDATE revisions SET extraction=?,task_status=?,error=? WHERE id=?",
                (dumps(validated), status, "仍有失败块待处理" if pending else None, rid),
            )
            self.save(
                db,
                record,
                meta,
                user,
                "MANUAL_BLOCK",
                command.note,
                {"chunk_id": cid, "before": before, "added": part, "no_facts": command.no_facts},
            )
        return self.store.get_revision(rid)

    def publish_group(self, rid, gid, command, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.editable(db, rid, command, allow_frozen=True)
            group = next((g for g in fact_groups(record) if g["id"] == gid), None)
            if not group or group["status"] != "APPROVED":
                raise ServiceError("事实组尚未审核通过", 409)
            decision = group["decision"]
            if decision["fingerprint"] != fingerprint(
                {"extraction": record["extraction"], "chunks": record["chunks"]}
            ):
                raise ServiceError("审核后内容已变化，请重新审核", 409)
            snapshot_id = decision.get("publication_revision_id")
            if snapshot_id:
                snapshot = self.store.get_revision(snapshot_id, db)
                if snapshot["knowledge_status"] == "PUBLISHED":
                    return snapshot
                if snapshot["knowledge_status"] != "APPROVED":
                    raise ServiceError("快照正在发布或已停用，请刷新", 409)
            else:
                snapshot_id = uuid.uuid4().hex
                extraction, chunks, mapping = publication_content(record, group, snapshot_id)
                extraction = self.schema.validate(
                    Extraction.model_validate(extraction), [Chunk.model_validate(c) for c in chunks]
                ).model_dump()
                metadata = {
                    k: v
                    for k, v in (record["parsed_metadata"] or {}).items()
                    if not k.startswith("_manual") and k not in ("_extraction", "_fact_review")
                }
                metadata["_group_publication"] = {
                    "source_revision_id": rid,
                    "source_version": record["version"],
                    "group_id": gid,
                    "title": group["title"],
                    "chunks": mapping,
                    "review": decision,
                }
                if "directory" in metadata:
                    metadata["directory"] = copy.deepcopy(metadata["directory"])
                    metadata["directory"]["rows"] = [
                        {**row, "chunk_id": new_id}
                        for row in metadata["directory"].get("rows", [])
                        for new_id, span in mapping.items()
                        if span["chunk_id"] == row["chunk_id"]
                    ]
                number = db.execute(
                    "SELECT MAX(number)+1 FROM revisions WHERE document_id=?", (record["document_id"],)
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO revisions(id,document_id,number,task_status,knowledge_status,text,chunks,parsed_metadata,extraction,model,extracted_at,created_at) VALUES(?,?,?,'READY','APPROVED',?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        record["document_id"],
                        number,
                        record["text"],
                        dumps(chunks),
                        dumps(metadata),
                        dumps(extraction),
                        record["model"],
                        record["extracted_at"],
                        now(),
                    ),
                )
                self.store.audit(
                    db, snapshot_id, user.name, "GROUP_SNAPSHOT", command.note, metadata["_group_publication"]
                )
                decision["publication_revision_id"] = snapshot_id
                meta = record["parsed_metadata"]
                meta["_fact_review"]["groups"][gid] = decision
                self.save(
                    db,
                    record,
                    meta,
                    user,
                    "GROUP_PUBLISH",
                    command.note,
                    {"group_id": gid, "snapshot_id": snapshot_id},
                )
                snapshot = self.store.get_revision(snapshot_id, db)
            db.execute(
                "UPDATE revisions SET knowledge_status='PUBLISHING',version=version+1 WHERE id=?",
                (snapshot_id,),
            )
        try:
            self.graph.publish(snapshot)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ServiceError) else "图发布失败，请检查服务后重试"
            with self.store.connect() as db:
                db.execute(
                    "UPDATE revisions SET knowledge_status='APPROVED',error=? WHERE id=?",
                    (message, snapshot_id),
                )
                self.store.audit(db, snapshot_id, user.name, "PUBLISH_FAILED", message)
            raise ServiceError(message, 502) from exc
        with self.store.connect() as db:
            db.execute(
                "UPDATE revisions SET knowledge_status='PUBLISHED',error=NULL WHERE id=?", (snapshot_id,)
            )
            self.store.audit(db, snapshot_id, user.name, "PUBLISHED", "事实组图写入与回读校验通过")
        return self.store.get_revision(snapshot_id)

    def copy_draft(self, rid, command, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.store.get_revision(rid, db)
            if record["version"] != command.expected_version or not command.note.strip():
                raise ServiceError("版本已变化或缺少修订依据", 409)
            if record["task_status"] not in ("READY", "PARTIAL", "FAILED") or not record["chunks"]:
                raise ServiceError("请等待解析结束后创建修订草稿", 409)
            if (record.get("parsed_metadata") or {}).get("_group_publication"):
                raise ServiceError("请从原审核草稿创建修订，避免丢失未发布事实", 409)
            new_id = uuid.uuid4().hex
            number = db.execute(
                "SELECT MAX(number)+1 FROM revisions WHERE document_id=?", (record["document_id"],)
            ).fetchone()[0]
            meta = copy.deepcopy(record["parsed_metadata"] or {})
            meta["_fact_review"] = {"items": {}, "groups": {}}
            meta["_review_copy"] = {"source_revision_id": rid}
            # 文本块继续指向同一原件；新发布快照会另生成全局唯一的图块 ID。
            manual_baseline(record, record["extraction"] or Extraction().model_dump(), meta)
            db.execute(
                "INSERT INTO revisions(id,document_id,number,task_status,text,chunks,parsed_metadata,extraction,model,extracted_at,created_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    record["document_id"],
                    number,
                    record["task_status"],
                    record["text"],
                    dumps(record["chunks"]),
                    dumps(meta),
                    dumps(record["extraction"]) if record["extraction"] else None,
                    record["model"],
                    record["extracted_at"],
                    now(),
                    record["error"],
                ),
            )
            self.store.audit(db, new_id, user.name, "REVIEW_COPY", command.note, {"source_revision_id": rid})
        return self.store.get_revision(new_id)
