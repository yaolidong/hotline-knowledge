"""跨文档实体的可撤销审核关联。事实保留原版本，绝不以相似分数自动合并。"""

import hashlib
import json

from pydantic import Field
from typing import Literal

from hotline.models import StrictModel, stable_id
from hotline.retrieval import bigrams
from hotline.semantics import canonical_name
from hotline.settings import ServiceError
from hotline.store import dumps

IDENTITY_TYPES = {
    "ServiceItem",
    "GovernmentDepartment",
    "PolicyDocument",
    "LegalBasis",
    "AdministrativeRegion",
}


class IdentityDecision(StrictModel):
    source_revision: str = Field(min_length=1, max_length=100)
    source_entity: str = Field(min_length=1, max_length=100)
    target_revision: str = Field(min_length=1, max_length=100)
    target_entity: str = Field(min_length=1, max_length=100)
    source_version: int = Field(ge=1)
    target_version: int = Field(ge=1)
    expected_event_id: int = Field(0, ge=0)
    action: Literal["confirm", "revoke"]
    note: str = Field(min_length=1, max_length=1000)


def fingerprint(entity):
    return hashlib.sha256(dumps(entity).encode()).hexdigest()


class IdentityService:
    def __init__(self, store):
        self.store = store

    def entity(self, rid, eid, db=None):
        record = self.store.get_revision(rid, db)
        entity = next((e for e in (record["extraction"] or {}).get("entities", []) if e["id"] == eid), None)
        if entity is None:
            raise ServiceError("关联实体不存在", 404)
        return record, entity

    @staticmethod
    def pair_key(left, right):
        return stable_id(*sorted([left, right]))

    def events(self, db):
        rows = db.execute(
            "SELECT id,snapshot,actor,created_at FROM audit WHERE action='IDENTITY' ORDER BY id"
        )
        latest = {}
        for row in rows:
            event = {
                **json.loads(row["snapshot"]),
                "event_id": row["id"],
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
            latest[event["key"]] = event
        return latest

    def associations(self, rid, eid):
        with self.store.connect() as db:
            events = self.events(db)
            result = []
            for event in events.values():
                refs = event["entities"]
                if not any(x["revision_id"] == rid and x["entity_id"] == eid for x in refs):
                    continue
                active = event["action"] == "confirm"
                published = True
                for ref in refs:
                    try:
                        record, entity = self.entity(ref["revision_id"], ref["entity_id"], db)
                        active &= fingerprint(entity) == ref["fingerprint"] and record[
                            "knowledge_status"
                        ] not in {"RETIRED", "REJECTED"}
                        published &= record["knowledge_status"] == "PUBLISHED"
                    except ServiceError:
                        active = published = False
                result.append({**event, "active": bool(active), "both_published": bool(published)})
            return result

    def _published_pair(self, event, db):
        if event["action"] != "confirm":
            return None
        pair = []
        for ref in event["entities"]:
            try:
                record, entity = self.entity(ref["revision_id"], ref["entity_id"], db)
            except ServiceError:
                return None
            if record["knowledge_status"] != "PUBLISHED" or fingerprint(entity) != ref["fingerprint"]:
                return None
            pair.append((record, entity))
        (left, a), (right, b) = pair
        if (
            a["type"] != "ServiceItem"
            or b["type"] != "ServiceItem"
            or not left["metadata"].get("region")
            or left["metadata"]["region"] != right["metadata"].get("region")
            or bool(left["metadata"].get("synthetic")) != bool(right["metadata"].get("synthetic"))
        ):
            return None
        return pair

    def expand_services(self, nodes, sources, graph):
        """只扩展种子事项的一层直接审核关联，不传递身份等价，也不改写图节点。"""
        seeds = {node["id"] for node in nodes}
        wanted, pending = set(), []
        with self.store.connect() as db:
            for event in self.events(db).values():
                refs = event["entities"]
                graph_ids = [stable_id(r["revision_id"], r["entity_id"]) for r in refs]
                if not seeds.intersection(graph_ids) or not all(r["revision_id"] in sources for r in refs):
                    continue
                pair = self._published_pair(event, db)
                if pair is None:
                    continue
                wanted.update(graph_ids)
                pending.append(
                    {
                        "key": event["key"],
                        "event_id": event["event_id"],
                        "entities": [
                            {"revision_id": r["id"], "name": e["name"], "graph_id": gid}
                            for (r, e), gid in zip(pair, graph_ids)
                        ],
                    }
                )
        wanted -= seeds
        if not wanted:
            return nodes, []
        if len(wanted) > 30:
            raise ServiceError("直接审核关联超过 30 个事项，请缩小关联范围后查询", 409)
        records = list({ref["revision_id"] for link in pending for ref in link["entities"]})
        candidates, overflow = graph.service_candidates(records)
        if overflow:
            raise ServiceError("关联来源图候选超过上限，不能保证完整性", 409)
        added = [
            n
            for n in candidates
            if n["id"] in wanted
            and n.get("recordId") in sources
            and n.get("region") == sources[n["recordId"]]["region"]
        ]
        available = seeds | {n["id"] for n in added}
        links = [link for link in pending if all(ref["graph_id"] in available for ref in link["entities"])]
        allowed = {ref["graph_id"] for link in links for ref in link["entities"]}
        return [*nodes, *(n for n in added if n["id"] in allowed)], links

    def validate_links(self, links):
        if not links:
            return
        with self.store.connect() as db:
            latest = self.events(db)
            for link in links:
                event = latest.get(link["key"])
                if (
                    not event
                    or event["event_id"] != link["event_id"]
                    or self._published_pair(event, db) is None
                ):
                    raise ServiceError("回答期间实体关联已撤销或来源已变化，本次回答作废，请重新查询", 409)

    def candidates(self, rid, eid):
        record, entity = self.entity(rid, eid)
        associations = self.associations(rid, eid)
        if entity["type"] not in IDENTITY_TYPES:
            return {
                "items": [],
                "associations": associations,
                "has_more": False,
                "message": "条件、材料和流程保留各自事实身份，不参与通用实体同一性关联。",
            }
        region = record["metadata"].get("region")
        if not region:
            return {
                "items": [],
                "associations": associations,
                "has_more": False,
                "message": "请先核对适用地区，未知地区不参与实体关联。",
            }
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT r.id FROM revisions r JOIN documents d ON d.id=r.document_id
                WHERE r.task_status='READY' AND r.knowledge_status IN ('DRAFT','APPROVED','PUBLISHED')
                AND d.id<>? AND json_extract(d.metadata,'$.region')=?
                AND COALESCE(json_extract(d.metadata,'$.synthetic'),0)=? ORDER BY r.created_at DESC LIMIT 201""",
                (record["document_id"], region, int(bool(record["metadata"].get("synthetic")))),
            ).fetchall()
        matches = []
        left_names = {canonical_name(entity["name"]), *(canonical_name(a) for a in entity.get("aliases", []))}
        attrs = entity.get("attributes", {})
        for row in rows[:200]:
            other = self.store.get_revision(row["id"])
            for target in other["extraction"]["entities"]:
                if target["type"] != entity["type"]:
                    continue
                right_attrs = target.get("attributes", {})
                keys = ("level", "service_code", "region_code", "document_number")
                if any(attrs.get(k) and right_attrs.get(k) and attrs[k] != right_attrs[k] for k in keys):
                    continue
                names = {
                    canonical_name(target["name"]),
                    *(canonical_name(a) for a in target.get("aliases", [])),
                }
                code = any(attrs.get(k) and attrs[k] == right_attrs.get(k) for k in keys[1:])
                equal = bool(left_names & names)
                words1, words2 = bigrams(entity["name"]), bigrams(target["name"])
                similarity = len(words1 & words2) / max(1, len(words1 | words2))
                if not (code or equal or similarity >= 0.4):
                    continue
                matches.append(
                    {
                        "revision_id": other["id"],
                        "entity_id": target["id"],
                        "name": target["name"],
                        "type": target["type"],
                        "region": region,
                        "title": other["metadata"].get("title") or other["filename"],
                        "version": other["version"],
                        "status": other["knowledge_status"],
                        "evidence": target["evidence"],
                        "score": 3 if code else 2 if equal else 1,
                        "reason": "同地区同类型，明确编码一致"
                        if code
                        else "同地区同类型，名称或明示别名一致"
                        if equal
                        else "同地区同类型，名称相近，仅供人工核对",
                    }
                )
        matches.sort(key=lambda x: (-x["score"], x["name"], x["revision_id"]))
        return {
            "items": matches[:20],
            "has_more": len(rows) > 200 or len(matches) > 20,
            "associations": associations,
            "source_version": record["version"],
            "message": "候选相似不代表同一实体。确认只关联身份，不合并政策事实、地区或历史版本。",
        }

    def decide(self, request, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            left, a = self.entity(request.source_revision, request.source_entity, db)
            right, b = self.entity(request.target_revision, request.target_entity, db)
            if (left["version"], right["version"]) != (request.source_version, request.target_version):
                raise ServiceError("关联资料已更新，请刷新后重新核对", 409)
            key = self.pair_key(left["id"] + ":" + a["id"], right["id"] + ":" + b["id"])
            previous = self.events(db).get(key, {})
            if previous.get("event_id", 0) != request.expected_event_id:
                raise ServiceError("关联审核记录已变化，请刷新", 409)
            if request.action == "confirm":
                if (
                    a["type"] not in IDENTITY_TYPES
                    or a["type"] != b["type"]
                    or left["document_id"] == right["document_id"]
                    or not left["metadata"].get("region")
                    or left["metadata"].get("region") != right["metadata"].get("region")
                    or bool(left["metadata"].get("synthetic")) != bool(right["metadata"].get("synthetic"))
                ):
                    raise ServiceError("只能关联不同文档中同地区、同类型的业务实体", 409)
                for record in (left, right):
                    if record["task_status"] != "READY" or record["knowledge_status"] not in {
                        "DRAFT",
                        "APPROVED",
                        "PUBLISHED",
                    }:
                        raise ServiceError("关联资料状态不允许确认", 409)
                for field in ("level", "service_code", "region_code", "document_number"):
                    av, bv = a.get("attributes", {}).get(field), b.get("attributes", {}).get(field)
                    if av and bv and av != bv:
                        raise ServiceError("实体编码或层级冲突，不能确认同一身份", 409)
            elif not previous or previous.get("action") != "confirm":
                raise ServiceError("没有可以撤销的确认记录", 409)
            event = {
                "key": key,
                "action": request.action,
                "note": request.note,
                "entities": [
                    {
                        "revision_id": r["id"],
                        "entity_id": e["id"],
                        "name": e["name"],
                        "fingerprint": fingerprint(e),
                    }
                    for r, e in ((left, a), (right, b))
                ],
            }
            self.store.audit(db, left["id"], user.name, "IDENTITY", request.note, event)
        return {"associations": self.associations(left["id"], a["id"])}
