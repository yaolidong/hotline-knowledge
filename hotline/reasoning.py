"""有界事实路径与条件三值核对。路径连接不构成新政策，核对结果不构成行政决定。"""

import hashlib
from datetime import date
from typing import Literal

from pydantic import Field

from hotline.models import Question
from hotline.query import temporal_status
from hotline.settings import ServiceError
from hotline.store import dumps


class PathRequest(Question):
    plan: Literal["legal_basis"] = "legal_basis"


class ConditionRequest(Question):
    observations: dict[str, Literal["TRUE", "FALSE", "UNKNOWN"]] = Field(default_factory=dict)
    rule_set_hash: str = Field("", max_length=64)


def evaluate_rule(rule, observations):
    if "condition" in rule:
        return {"condition": rule["condition"], "value": observations.get(rule["condition"], "UNKNOWN")}
    children = [evaluate_rule(child, observations) for child in rule["conditions"]]
    values = [child["value"] for child in children]
    operator = rule["operator"]
    if operator == "NOT":
        value = {"TRUE": "FALSE", "FALSE": "TRUE", "UNKNOWN": "UNKNOWN"}[values[0]]
    elif operator == "AND":
        value = "FALSE" if "FALSE" in values else "TRUE" if all(v == "TRUE" for v in values) else "UNKNOWN"
    else:
        value = "TRUE" if "TRUE" in values else "FALSE" if all(v == "FALSE" for v in values) else "UNKNOWN"
    return {"operator": operator, "value": value, "conditions": children, "basis": rule.get("basis", "")}


def leaves(rule):
    if "condition" in rule:
        return {rule["condition"]}
    return set().union(*(leaves(child) for child in rule["conditions"]))


class ReasoningService:
    def __init__(self, store, graph, query):
        self.store, self.graph, self.query = store, graph, query

    def paths(self, request):
        result = self.query.ask(request)
        if result["status"] == "NEEDS_CLARIFICATION" or not result["services"]:
            return {"status": result["status"], "message": result["answer"], "paths": [], "query": result}
        space = result["space"]
        visible = self.store.published_sources(space)
        ids = {s["entity_id"] for s in result["services"]}
        regions = {s["region"] for s in result["services"]}
        cache, paths, truncated = {}, [], False
        frontier = [([], seed) for seed in ids]
        # 唯一模板：事项→依据，或事项→条件→条款→依据；最长三条原文关系。
        for depth in range(3):
            if not frontier:
                break
            rows, overflow = self.graph.facts(list(visible), list({node for _, node in frontier}))
            if overflow:
                truncated = True
                break
            next_frontier = []
            for chain, node_id in frontier:
                for row in rows:
                    if row["source"]["id"] != node_id:
                        continue
                    kind = row["type"]
                    spec = self.query.schema.relations.get(kind, {})
                    if row["source"].get("entityType") not in spec.get("sources", []) or row["target"].get(
                        "entityType"
                    ) not in spec.get("targets", []):
                        continue
                    if kind != "BASED_ON" and not (depth == 0 and kind == "REQUIRES_CONDITION"):
                        continue
                    rid = row["source"].get("recordId")
                    if rid not in visible or row["target"].get("recordId") != rid:
                        continue
                    if (
                        row["source"].get("region") not in regions
                        or row["target"].get("region") not in regions
                    ):
                        continue
                    if row["target"]["id"] in [step["source_id"] for step in chain]:
                        continue
                    if rid not in cache:
                        cache[rid] = self.store.get_revision(rid)
                    record = cache[rid]
                    if (
                        record["knowledge_status"] != "PUBLISHED"
                        or temporal_status(record, date.fromisoformat(result["as_of"]))["status"]
                        != "UNCONFIRMED"
                    ):
                        continue
                    chunks = {c["id"]: c for c in record["chunks"]}
                    evidence = []
                    for item in row["relation"].get("evidence", []):
                        chunk = chunks.get(item.get("chunk_id"))
                        if chunk and chunk["text"][item.get("start", 0) : item.get("end", 0)] == item.get(
                            "quote"
                        ):
                            evidence.append(
                                {
                                    **item,
                                    "revision_id": rid,
                                    "title": record["metadata"].get("title") or record["filename"],
                                    "page": chunk["page"],
                                    "section": chunk["section"],
                                }
                            )
                    if not evidence:
                        continue
                    path = [
                        *chain,
                        {
                            "source": row["source"]["name"],
                            "source_id": node_id,
                            "target": row["target"]["name"],
                            "target_id": row["target"]["id"],
                            "relation": kind,
                            "record_id": rid,
                            "evidence": evidence,
                        },
                    ]
                    if kind == "BASED_ON":
                        paths.append(path)
                        if len(paths) >= 50:
                            truncated = True
                            break
                    if row["target"].get("entityType") in {"EligibilityCondition", "PolicyProvision"}:
                        next_frontier.append((path, row["target"]["id"]))
                if truncated:
                    break
            if truncated:
                break
            frontier = next_frontier[:100]
            if len(next_frontier) > 100:
                truncated = True
                break
        if not set(cache).issubset(set(self.store.visible_ids(space=space))):
            raise ServiceError("路径来源已变化，请重新查询", 409)
        self.query.identity.validate_links(result.get("retrieval", {}).get("identity_links", []))
        return {
            "status": "FOUND" if paths else "NO_EVIDENCE",
            "plan": request.plan,
            "paths": paths,
            "has_more": truncated,
            "max_hops": 3,
            "query": result,
            "message": "每条边来自已发布图谱及原文。路径只说明关联依据，不自动推导新的政策要求；现行有效性仍需核实。",
        }

    def conditions(self, request):
        # 使用固定领域意图，避免把调用方任意问题当成条件树选择器。
        scoped = Question.model_validate(
            {**request.model_dump(exclude={"observations", "rule_set_hash"}), "question": "查询办理条件"}
        )
        if not scoped.service:
            raise ServiceError("请先选择明确的事项和地区，再核对条件")
        result = self.query.ask(scoped)
        rules = [f for f in result["facts"] if f["relation"] == "REQUIRES_CONDITION" and f.get("rule")]
        if result["status"] != "ANSWERED" or not rules:
            return {
                "status": "UNAVAILABLE",
                "message": "没有可安全核对的明确条件树，或来源存在差异；请核对原文。",
                "query": result,
                "rules": [],
            }
        fingerprint = hashlib.sha256(dumps(rules).encode()).hexdigest()
        valid_leaves = set().union(*(leaves(f["rule"]) for f in rules))
        if len(valid_leaves) > 200 or not set(request.observations).issubset(valid_leaves):
            raise ServiceError("条件观察项超限或不属于当前规则，请重新加载")
        if request.observations and request.rule_set_hash != fingerprint:
            raise ServiceError("条件或证据版本已变化，请重新加载后核对", 409)
        return {
            "status": "CHECKED",
            "rule_set_hash": fingerprint,
            "leaves": sorted(valid_leaves),
            "rules": [
                {**fact, "evaluation": evaluate_rule(fact["rule"], request.observations)} for fact in rules
            ],
            "query": result,
            "message": "仅核对已检索到的明确条件表达式。输入由客服根据已确认信息填写，未知不会当作不符合；结果不代表完整资格判断或行政决定。",
        }
