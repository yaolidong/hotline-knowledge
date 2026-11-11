import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from hotline.models import Question
from hotline.retrieval import resolve_services
from hotline.schema import DomainSchema
from hotline.settings import ServiceError
from hotline.temporal import query_date
from hotline.evidence import locate_evidence
from hotline.semantics import step_order
from hotline.identity import IdentityService


def parse_date(value):
    match = re.fullmatch(r"\s*(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?\s*", value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def temporal_status(record, as_of):
    """仅在有明示失效/未生效证据时给出确定状态；绝不返回「现行有效」。"""
    values = record.get("parsed_metadata") or {}
    result = {"status": "UNCONFIRMED", "message": "现行有效性尚待核实", "dates": values}
    for field in ("expires_on", "repealed_on"):
        info = values.get(field, {})
        value = parse_date(info.get("value", ""))
        expired = value and (as_of > value if info.get("label") == "有效期至" else as_of >= value)
        if expired:
            return {**result, "status": "EXPIRED", "message": "已到原文明示的失效或废止日期"}
    effective = parse_date(values.get("effective_on", {}).get("value", ""))
    if effective and as_of < effective:
        return {**result, "status": "NOT_YET_EFFECTIVE", "message": "尚未到原文明示的施行日期"}
    # 有施行日且已过施行日仍不足以证明现行有效（可能已被未导入文件废止）。
    return result


def classify_conflicts(grouped: dict) -> list[dict]:
    """将来源集合差异细分为互补 / 待核对 / 有证据互斥，不静默选边。"""
    items = []
    for relation, versions in grouped.items():
        sets = {rid: frozenset(names) for rid, names in versions.items()}
        unique = {tuple(sorted(v)) for v in sets.values()}
        if len(unique) <= 1:
            continue
        values = list(sets.values())
        kind = "pending_review"
        message = "多个已发布来源的要求不同，请核对地区、版本及完整原文；系统不自行选择其中一个。"
        if all(a <= b or b <= a for a in values for b in values):
            kind = "complementary"
            message = "多个来源要求存在包含关系，可能为补充表述；请核对是否需同时满足。"
        elif relation in {"REQUIRES_MATERIAL", "REQUIRES_CONDITION", "HANDLED_BY"} and all(
            a.isdisjoint(b) for i, a in enumerate(values) for b in values[i + 1 :]
        ):
            kind = "mutually_exclusive"
            message = "多个已发布来源给出互斥要求，存在证据冲突；请人工核对后答复，系统不选边。"
        items.append(
            {
                "relation": relation,
                "kind": kind,
                "message": message,
                "sources": {rid: sorted(names) for rid, names in sets.items()},
            }
        )
    return items


def organize_facts(facts, intentions):
    """同一证据下完整条件已覆盖的子项不重复展示；保留数据库中的独立断言。"""

    def evidence_keys(fact):
        return {(e["chunk_id"], e["quote"]) for e in fact["evidence"]}

    complete = [f for f in facts if f["relation"] == "REQUIRES_CONDITION" and f.get("rule")]
    result = []
    for fact in facts:
        covered = (
            fact["relation"] == "REQUIRES_CONDITION"
            and not fact.get("rule")
            and any(
                full["record_id"] == fact["record_id"]
                and fact["value"] in full["value"]
                and evidence_keys(fact)
                and evidence_keys(fact).issubset(evidence_keys(full))
                for full in complete
            )
        )
        if not covered:
            result.append(fact)
    return sorted(
        result,
        key=lambda fact: (
            fact["relation"] not in intentions,
            fact.get("sequence") is None,
            fact.get("sequence") or 0,
        ),
    )


class QueryService:
    def __init__(self, store, graph, schema: DomainSchema, default_space="production"):
        self.store, self.graph, self.schema = store, graph, schema
        self.default_space = default_space
        self.identity = IdentityService(store)

    def ask(self, request: Question):
        start = time.perf_counter()
        request = request.model_copy(update={"space": request.space or self.default_space})
        today = datetime.now(timezone(timedelta(hours=8))).date()
        today, date_source, date_message = query_date(request.question, request.as_of, today)
        result = {
            "answer": "没有检索到可用于回答的已发布知识。",
            "status": "NO_EVIDENCE",
            "facts": [],
            "citations": [],
            "warnings": [],
            "services": [],
            "answer_kind": "原文事实的结构化整理",
            "as_of": today.isoformat(),
            "date_resolution": date_source,
            "space": request.space,
        }
        if date_message:
            result.update(status="NEEDS_CLARIFICATION", answer=date_message)
            return self._done(result, start)
        sources = self.store.published_sources(request.space)
        records = list(sources)
        if not records:
            return self._done(result, start)
        resolution = resolve_services(self.graph, request, sources)
        request = resolution.request
        services = resolution.nodes
        result["scope"] = {"region": request.region, "service": request.service}
        if request.service_code:
            result["scope"]["service_code"] = request.service_code
        result["retrieval"] = {"method": resolution.method}
        regions = {node.get("region", "") for node in services}
        names = {node["name"] for node in services}
        result["services"] = [
            {
                "name": n["name"],
                "region": n.get("region", ""),
                "record_id": n["recordId"],
                "entity_id": n["id"],
                "service_code": n.get("attrs", {}).get("service_code", ""),
                "title": sources[n["recordId"]]["title"] or sources[n["recordId"]]["filename"],
            }
            for n in services
        ]
        if not {n["recordId"] for n in services}.issubset(set(self.store.visible_ids())):
            raise ServiceError("查询期间知识状态已变化，请重试", 409)
        if resolution.clarification:
            result.update(status="NEEDS_CLARIFICATION", answer=resolution.clarification)
            return self._done(result, start)
        if not services:
            if resolution.method == "NO_REGION_MATCH":
                result["answer"] = (
                    f"当前未找到适用于{request.region}的已发布资料，请核对地区或补充该地区知识。"
                )
            else:
                result["answer"] = (
                    "当前缺少相关已发布资料或事项名称未匹配。"
                    "请确认查询范围；若资料仍在抽取/审核中，需知识维护人员发布后才能查询。"
                    "客服可见提示仅此合规说明，详细过滤原因请维护人员使用检索诊断。"
                )
            return self._done(result, start)
        codes = {node.get("attrs", {}).get("service_code", "") for node in services}
        if len(codes) > 1:
            result.update(
                status="NEEDS_CLARIFICATION",
                answer="同名事项存在不同编码或尚未核对编码的来源，请选择具体实施事项，避免混合办理要求。",
            )
            return self._done(result, start)
        if len(regions) > 1 or len(names) > 1 or "" in regions:
            result.update(
                status="NEEDS_CLARIFICATION",
                answer="请确认唯一事项及适用地区；地区未知的知识须由维护人员补充后重新审核。",
            )
            return self._done(result, start)
        services, links = self.identity.expand_services(services, sources, self.graph)
        if links:
            result["retrieval"]["identity_links"] = links
            known = {s["entity_id"] for s in result["services"]}
            result["services"].extend(
                {
                    "name": n["name"],
                    "region": n["region"],
                    "record_id": n["recordId"],
                    "entity_id": n["id"],
                    "service_code": n.get("attrs", {}).get("service_code", ""),
                    "title": sources[n["recordId"]]["title"] or sources[n["recordId"]]["filename"],
                }
                for n in services
                if n["id"] not in known
            )
        rows, overflow = self.graph.facts(records, [s["id"] for s in services])
        if overflow:
            result.update(
                status="NEEDS_CLARIFICATION", answer="关联事实过多，无法保证材料和条件完整，请缩小查询范围。"
            )
            return self._done(result, start)
        intentions = [
            rel
            for rel, words in self.schema.data["intents"].items()
            if any(word in request.question for word in words)
        ]
        validity_question = any(word in request.question for word in ("有效", "废止", "失效", "现行"))
        overview = any(
            word in request.question for word in ("办理信息", "办事指南", "怎么办理", "介绍", "全部要求")
        )
        if not intentions and not validity_question and not overview:
            result.update(
                answer="尚未支持从已发布图谱中回答这一问题，请明确询问部门、材料、条件、流程、渠道或政策依据。"
            )
            return self._done(result, start)
        cited = {}
        temporal = {}
        facts = []
        grouped = defaultdict(lambda: defaultdict(set))
        for row in rows:
            rid = row["source"]["recordId"]
            if rid not in cited:
                cited[rid] = self.store.get_revision(rid)
                temporal[rid] = temporal_status(cited[rid], today)
            # 地区与发布白名单双重校验，即使后端返回意外数据也不能混答。
            record = cited[rid]
            if record["knowledge_status"] != "PUBLISHED" or row["target"].get("region") not in regions:
                continue
            if temporal[rid]["status"] != "UNCONFIRMED":
                result["warnings"].append(temporal[rid]["message"] + "：" + record["filename"])
                continue
            relation_evidence = row["relation"].get("evidence", [])
            chunks = {c["id"]: c for c in record["chunks"]}
            evidence = []
            for item in relation_evidence:
                chunk = chunks.get(item.get("chunk_id"))
                if not chunk or chunk["text"][item.get("start", 0) : item.get("end", 0)] != item.get("quote"):
                    continue
                evidence.append(
                    {
                        **item,
                        "page": chunk["page"],
                        "section": chunk["section"],
                        "absolute_start": chunk["start"] + item["start"],
                        "revision_id": rid,
                        "document_id": record["document_id"],
                        "title": record["metadata"].get("title") or record["filename"],
                        "source": record["metadata"].get("source", ""),
                        "synthetic": record["metadata"].get("synthetic", False),
                        "locator": locate_evidence(record, chunk, item["start"], item["end"]),
                    }
                )
            if not evidence:
                continue
            relation = row["type"]
            target = row["target"]
            grouped[relation][rid].add(target["name"])
            if intentions and relation not in intentions and relation != "REQUIRES_CONDITION":
                continue
            fact = {
                "relation": relation,
                "label": self.schema.relations.get(relation, {}).get("label", relation),
                "value": target["name"],
                "attributes": target.get("attrs", {}),
                "rule": target.get("rule"),
                "sequence": target.get("metadata", {}).get("sequence")
                or step_order(target.get("attrs", {}).get("order", "")),
                "evidence": evidence,
                "record_id": rid,
                "review_status": "PUBLISHED",
            }
            facts.append(fact)
        facts = organize_facts(facts, intentions)
        conflict_items = classify_conflicts(grouped)
        if conflict_items:
            result["conflicts"] = conflict_items
            for item in conflict_items:
                result["warnings"].append(item["message"])
        if any(record["metadata"].get("synthetic") for record in cited.values()):
            result["warnings"].append("当前结果包含合成测试资料，不能作为真实政策使用。")
        result["warnings"].append("尚无足够证据确认政策当前有效；涉及个案资格时需进一步核实。")
        # 读取事实期间如发生停用，整批回答作废，避免继续使用已撤回资料。
        if not set(cited).issubset(set(self.store.visible_ids())):
            raise ServiceError("查询期间知识状态已变化，请重试", 409)
        if validity_question:
            result.update(
                status="VALIDITY_UNCONFIRMED",
                answer="不能仅凭文件发布日期或已过施行日判断当前必然有效，请核实施行、失效及明确废止依据。",
                validity=[{"record_id": rid, **value} for rid, value in temporal.items()],
            )
        elif facts and (not intentions or any(f["relation"] in intentions for f in facts)):
            hard = any(c["kind"] == "mutually_exclusive" for c in conflict_items)
            soft = bool(conflict_items)
            result.update(
                status="CONFLICT" if hard or soft else "ANSWERED",
                facts=facts,
                answer="；".join(dict.fromkeys(f"{f['label']}：{f['value']}" for f in facts)),
            )
        result["citations"] = [item for fact in result["facts"] for item in fact["evidence"]]
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        return self._done(result, start)

    def _done(self, result, start):
        self.identity.validate_links(result.get("retrieval", {}).get("identity_links", []))
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return result
