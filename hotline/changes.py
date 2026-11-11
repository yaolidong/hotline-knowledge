"""版本差异与复核范围。抽取遗漏不等于政策删除，重跑不等于政策修订。"""

from collections import defaultdict
from difflib import SequenceMatcher

from hotline.models import stable_id
from hotline.semantics import canonical_name
from hotline.settings import ServiceError


def entity_key(entity):
    return entity["type"], canonical_name(entity["name"])


def signatures(extraction):
    names = {e["id"]: entity_key(e) for e in extraction.get("entities", [])}
    return {
        (r["type"], names.get(r["source"]), names.get(r["target"])) for r in extraction.get("relations", [])
    }


class ChangeService:
    def __init__(self, store, graph):
        self.store, self.graph = store, graph

    def compare(self, revision_id, before_id=""):
        after = self.store.get_revision(revision_id)
        if not after["extraction"]:
            raise ServiceError("当前版本尚无可比较的抽取结果", 409)
        if not before_id:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT id FROM revisions WHERE document_id=? AND number<? AND task_status='READY' "
                    "ORDER BY (knowledge_status='PUBLISHED') DESC,number DESC LIMIT 1",
                    (after["document_id"], after["number"]),
                ).fetchone()
            if not row:
                return {
                    "status": "NO_BASELINE",
                    "message": "没有此前完成的版本。可填写另一份同空间资料的版本 ID，显式进行来源比较。",
                }
            before_id = row["id"]
        before = self.store.get_revision(before_id)
        if not before["extraction"] or before_id == revision_id:
            raise ServiceError("请选择另一份有抽取结果的版本", 400)
        if bool(before["metadata"].get("synthetic")) != bool(after["metadata"].get("synthetic")):
            raise ServiceError("不能将正式资料与合成资料作为版本基线比较", 409)
        warnings = ["以下是原文及抽取结果差异，不代表政策已经修订、废止或替代。缺失节点也可能是抽取遗漏。"]
        if before["document_id"] != after["document_id"]:
            warnings.append("跨文件显式比较：尚未确认两个来源属于同一政策版本链。")
        if before["metadata"].get("region") != after["metadata"].get("region"):
            warnings.append("两份资料的地区不同，不能直接相互替代。")
        text_diff = []
        left_lines, right_lines = (before["text"] or "").splitlines(), (after["text"] or "").splitlines()
        # 行级有界差异，不对整篇中文做二次方字符匹配。
        if max(len(left_lines), len(right_lines)) > 20000:
            warnings.append("文本行数超过细粒度比较上限，以下仅标记整篇变化，请对照原件。")
            opcodes = [
                (
                    "equal" if before["text"] == after["text"] else "replace",
                    0,
                    len(left_lines),
                    0,
                    len(right_lines),
                )
            ]
        else:
            opcodes = SequenceMatcher(None, left_lines, right_lines, autojunk=True).get_opcodes()
        for tag, i, j, m, n in opcodes:
            if tag == "equal":
                continue
            left, right = "\n".join(left_lines[i:j]), "\n".join(right_lines[m:n])
            text_diff.append(
                {
                    "operation": tag,
                    "before_lines": [i + 1, j],
                    "after_lines": [m + 1, n],
                    "before": left[:4000],
                    "after": right[:4000],
                    "truncated": max(len(left), len(right)) > 4000,
                }
            )
        old, new = defaultdict(list), defaultdict(list)
        for e in before["extraction"]["entities"]:
            old[entity_key(e)].append(e)
        for e in after["extraction"]["entities"]:
            new[entity_key(e)].append(e)
        entities, ambiguous = [], []
        for key in sorted(old.keys() | new.keys()):
            a, b = old[key], new[key]
            if len(a) > 1 or len(b) > 1:
                ambiguous.append(
                    {"type": key[0], "name": key[1], "before_count": len(a), "after_count": len(b)}
                )
                continue
            if not a or not b:
                entities.append(
                    {
                        "operation": "added" if b else "absent",
                        "type": key[0],
                        "name": key[1],
                        "before": a[0] if a else None,
                        "after": b[0] if b else None,
                    }
                )
                continue
            changed = [
                field
                for field in ("name", "attributes", "aliases", "rule", "sequence_basis", "display_name")
                if a[0].get(field) != b[0].get(field)
            ]
            if changed:
                entities.append(
                    {
                        "operation": "changed",
                        "type": key[0],
                        "name": key[1],
                        "fields": changed,
                        "before": a[0],
                        "after": b[0],
                    }
                )
        old_edges, new_edges = signatures(before["extraction"]), signatures(after["extraction"])
        if ambiguous:
            warnings.append("存在同类型同名的多个实体，关系按名称比较不能区分其实例；相关差异需逐条核对。")
        relations = [
            {"operation": operation, "type": edge[0], "source": edge[1], "target": edge[2]}
            for operation, values in (("added", new_edges - old_edges), ("absent", old_edges - new_edges))
            for edge in sorted(values)
        ]
        affected = {}
        for record in (before, after):
            for entity in record["extraction"]["entities"]:
                if entity["type"] == "ServiceItem":
                    key = stable_id(record["metadata"].get("region", ""), canonical_name(entity["name"]))
                    affected[key] = {
                        "name": canonical_name(entity["name"]),
                        "region": record["metadata"].get("region", ""),
                        "reason": "所在资料存在差异，需要复核事项要求与证据",
                    }
        if not (text_diff or entities or relations or ambiguous):
            affected = {}
        space = "test" if after["metadata"].get("synthetic") else "production"
        published = self.store.published_sources(space)
        related = []
        graph_status, overflow = "CHECKED", False
        try:
            nodes, overflow = self.graph.service_candidates(list(published)) if affected else ([], False)
            for node in nodes:
                if node.get("recordId") in published and any(
                    node.get("region") == a["region"] and canonical_name(node["name"]) == a["name"]
                    for a in affected.values()
                ):
                    related.append(
                        {
                            "name": node["name"],
                            "region": node["region"],
                            "revision_id": node["recordId"],
                            "title": published[node["recordId"]]["title"],
                        }
                    )
        except ServiceError:
            graph_status = "UNAVAILABLE"
            warnings.append("图服务暂不可用，未完成已发布同名事项影响查询。原文差异仍可审阅。")
        if not {item["revision_id"] for item in related}.issubset(set(self.store.visible_ids(space=space))):
            raise ServiceError("影响分析期间知识状态变化，请重试", 409)
        if any(self.store.get_revision(r["id"])["version"] != r["version"] for r in (before, after)):
            raise ServiceError("比较期间版本内容发生修改，请重试", 409)
        same_file = before["sha256"] == after["sha256"]
        return {
            "status": "COMPARED",
            "classification": "REEXTRACTION"
            if same_file and before["text"] == after["text"]
            else "REPARSE"
            if same_file
            else "SOURCE_CHANGE",
            "before": self.summary(before),
            "after": self.summary(after),
            "warnings": warnings,
            "text_changes": text_diff[:100],
            "text_changes_total": len(text_diff),
            "entity_changes": entities[:200],
            "entity_changes_total": len(entities),
            "relation_changes": relations[:200],
            "relation_changes_total": len(relations),
            "ambiguous_entities": ambiguous[:100],
            "ambiguous_entities_total": len(ambiguous),
            "affected_services": list(affected.values()),
            "published_candidates": related[:100],
            "graph_status": graph_status,
            "graph_candidates_truncated": overflow or len(related) > 100,
            "has_more": len(text_diff) > 100
            or len(entities) > 200
            or len(relations) > 200
            or len(ambiguous) > 100,
        }

    @staticmethod
    def summary(record):
        return {
            key: record[key]
            for key in (
                "id",
                "document_id",
                "number",
                "version",
                "sha256",
                "knowledge_status",
                "model",
                "extracted_at",
                "filename",
            )
        }
