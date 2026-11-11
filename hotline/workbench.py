"""事项工作台和检索诊断，业务事实仍来自已发布的真实图查询。"""

from hotline.models import stable_id
from hotline.settings import ServiceError
from hotline.query import temporal_status
from datetime import date


class ServiceWorkbench:
    def __init__(self, store, graph, query, qa):
        self.store, self.graph, self.query, self.qa = store, graph, query, qa

    def services(self, space, keyword="", region="", limit=30, offset=0):
        records = self.store.published_sources(space)
        nodes, overflow = self.graph.service_candidates(list(records))
        groups = {}
        for node in nodes:
            rid = node.get("recordId")
            if rid not in records or node.get("region") != records[rid]["region"]:
                continue
            if region and region not in node.get("region", ""):
                continue
            if keyword and not any(keyword in x for x in [node["name"], *node.get("aliases", [])]):
                continue
            code = node.get("attrs", {}).get("service_code", "")
            key = stable_id(space, node["region"], node["name"], code)
            group = groups.setdefault(
                key,
                {
                    "id": key,
                    "name": node["name"],
                    "region": node["region"],
                    "service_code": code,
                    "sources": [],
                },
            )
            group["sources"].append(
                {
                    "revision_id": rid,
                    "title": records[rid]["title"] or records[rid]["filename"],
                    "attributes": node.get("attrs", {}),
                    "entity_id": node["id"],
                }
            )
        values = sorted(groups.values(), key=lambda x: (x["region"], x["name"]))
        if not {s["revision_id"] for g in values for s in g["sources"]}.issubset(
            set(self.store.visible_ids(space=space))
        ):
            raise ServiceError("事项来源状态已变化，请刷新", 409)
        return {
            "items": values[offset : offset + limit],
            "total": len(values),
            "has_more": overflow or offset + limit < len(values),
            "truncated": overflow,
            "space": space,
            "grouping": "同地区、同名、同编码来源展示，不表示身份已合并",
        }

    def diagnose(self, request):
        result = self.query.ask(request)
        scoped = request.model_copy(update={**result.get("scope", {}), "space": result["space"]})
        details = {}
        sources, clarification = [], ""
        if result["status"] not in {"NEEDS_CLARIFICATION", "VALIDITY_UNCONFIRMED"}:
            sources, clarification = self.qa._retrieve(scoped, result, details)
            self.qa._check_visible(sources)
        exclusions = []
        # 维护端按相关标题/事项展示过滤原因；只返回有上限的解释，不声称扫描全库。
        page = self.store.revision_page(100, 0, scoped.service or "")
        service_records = {s["record_id"] for s in result["services"]}
        for item in page["items"]:
            reasons = []
            if bool(item["metadata"].get("synthetic")) != (scoped.space == "test"):
                reasons.append("OTHER_SPACE")
            if item["knowledge_status"] != "PUBLISHED":
                reasons.append("NOT_PUBLISHED")
            if scoped.region and item["metadata"].get("region") != scoped.region:
                reasons.append("REGION_MISMATCH")
            if service_records and item["id"] not in service_records:
                reasons.append("OTHER_SERVICE_SOURCE")
            record = self.store.get_revision(item["id"])
            time_status = temporal_status(record, date.fromisoformat(result["as_of"]))["status"]
            if time_status != "UNCONFIRMED":
                reasons.append(time_status)
            if reasons:
                exclusions.append(
                    {
                        "revision_id": item["id"],
                        "title": item["metadata"].get("title") or item["filename"],
                        "reasons": reasons,
                    }
                )
        return {
            "result": result,
            "retrieval": details,
            "selected_sources": sources,
            "clarification": clarification,
            "exclusions": exclusions,
            "exclusions_truncated": page["total"] > 100,
            "generation_used": False,
            "message": "本次执行真实检索，未调用答案生成模型；排序值不代表政策正确性。",
        }
