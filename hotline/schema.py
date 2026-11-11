import json
from pathlib import Path

from hotline.models import Chunk, Extraction, stable_id
from hotline.settings import ServiceError
from hotline.semantics import canonical_name, flow_order

SCHEMA_PATH = Path(__file__).parent / "config" / "schema.json"


def spg_name(relation: str) -> str:
    # KAG 0.8 MarkLang 的谓词标识符不接受下划线。
    first, *rest = relation.lower().split("_")
    return first + "".join(word.title() for word in rest)


class DomainSchema:
    def __init__(self, path: Path = SCHEMA_PATH):
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.entities = self.data["entities"]
        self.relations = self.data["relations"]

    def validate(self, extraction: Extraction, chunks: list[Chunk]) -> Extraction:
        extraction = extraction.model_copy(deep=True)
        chunk_map = {chunk.id: chunk for chunk in chunks}
        entities = {entity.id: entity for entity in extraction.entities}
        if len(entities) != len(extraction.entities):
            raise ServiceError("抽取实体 ID 重复")
        items = [(f"实体 {i + 1}（{item.id}）", item) for i, item in enumerate(extraction.entities)]
        items += [(f"关系 {i + 1}", item) for i, item in enumerate(extraction.relations)]
        for label, item in items:
            for evidence_index, evidence in enumerate(item.evidence, 1):
                location = f"{label} 的第 {evidence_index} 条证据（chunk_id={evidence.chunk_id}）："
                chunk = chunk_map.get(evidence.chunk_id)
                if not chunk:
                    raise ServiceError(location + "证据引用了输入范围之外的文本块")
                if evidence.start is None:
                    if chunk.text.count(evidence.quote) != 1:
                        raise ServiceError(
                            location
                            + "证据原文不存在或出现多次，请提供准确偏移；逐字保留空格和换行，不要拼接不同段落"
                        )
                    evidence.start = chunk.text.index(evidence.quote)
                    evidence.end = evidence.start + len(evidence.quote)
                elif evidence.end is None:
                    evidence.end = evidence.start + len(evidence.quote)
                if (
                    not 0 <= evidence.start < evidence.end <= len(chunk.text)
                    or chunk.text[evidence.start : evidence.end] != evidence.quote
                ):
                    raise ServiceError(location + "证据文本与指定原文范围不一致")
        for index, entity in enumerate(extraction.entities):
            if entity.type not in self.entities:
                raise ServiceError("未知的实体类型")
            quotes = [e.quote for e in entity.evidence]
            if not entity.name.strip() or not any(entity.name in q for q in quotes):
                raise ServiceError(
                    f"第 {index + 1} 个实体名称必须来自其证据原文，空格、换行和原文字形也必须一致；"
                    f"候选名称：{entity.name[:120]!r}"
                )
            for alias in entity.aliases:
                if not alias.strip() or not any(alias in q for q in quotes):
                    raise ServiceError("别名必须有原文证据")
            entity.canonical_name = canonical_name(entity.name)
            if entity.sequence_basis and (
                entity.type != "ProcedureStep"
                or not any(entity.sequence_basis in q for q in quotes)
                or flow_order(entity.sequence_basis, entity.name) is None
            ):
                raise ServiceError("流程顺序依据必须是原文明示的连线序列，且唯一包含本步骤名称")
            if entity.display_name and not any(
                canonical_name(entity.display_name) in canonical_name(q) for q in quotes
            ):
                raise ServiceError(
                    f"第 {index + 1} 个实体的 display_name 展示名称必须来自原文片段，不能作为无依据的摘要；可留空"
                )
            allowed = self.entities[entity.type]["attributes"]
            for key, value in entity.attributes.items():
                if key not in allowed or not value.strip() or not any(value in q for q in quotes):
                    field = key if key in allowed else "未知字段"
                    raise ServiceError(
                        f"第 {index + 1} 个实体的 {field} 属性不符合 Schema 或缺乏直接原文依据；"
                        f"候选值：{value[:120]!r}。类别属性也必须采用原文表述，不能自行改写为分类标签。"
                    )
            if entity.rule is not None:
                if entity.type not in self.data.get("rule_entities", ["EligibilityCondition"]):
                    raise ServiceError("该实体类型不允许携带条件表达式")
                self.validate_rule(entity.rule, quotes)
        seen = set()
        for relation in extraction.relations:
            spec = self.relations.get(relation.type)
            source, target = entities.get(relation.source), entities.get(relation.target)
            if not spec or not source or not target:
                raise ServiceError("关系类型或端点不合法")
            if source.type not in spec["sources"] or target.type not in spec["targets"]:
                raise ServiceError(
                    f"关系端点类型违反政务 Schema：{relation.type} 收到 {source.type} → {target.type}；"
                    f"允许源类型 {','.join(spec['sources'])}，目标类型 {','.join(spec['targets'])}。"
                    "请按原文语义选择合法关系，不能仅为满足类型约束而改变实体含义。"
                )
            key = (relation.source, relation.type, relation.target)
            if key in seen:
                raise ServiceError("同一关系重复，请合并证据后提交")
            seen.add(key)
        return extraction

    def validate_rule(self, rule: dict, quotes: list[str], depth=0):
        if depth > 10:
            raise ServiceError("条件表达式嵌套过深")
        if set(rule) == {"condition"}:
            value = rule["condition"]
            if not isinstance(value, str) or not value.strip() or not any(value in q for q in quotes):
                raise ServiceError("条件叶子必须有原文证据")
            return
        if set(rule) not in ({"operator", "conditions"}, {"operator", "conditions", "basis"}):
            raise ServiceError("条件表达式字段不合法")
        operator, children = rule["operator"], rule["conditions"]
        if operator not in ("AND", "OR", "NOT") or not isinstance(children, list):
            raise ServiceError("条件表达式运算符不合法")
        if not 1 <= len(children) <= 100 or (operator == "NOT" and len(children) != 1):
            raise ServiceError("条件表达式子项数量不合法")
        if operator in ("AND", "OR") and len(children) < 2:
            raise ServiceError("AND/OR 至少需要两个子项")
        if "basis" in rule:
            basis = rule["basis"]
            markers = self.data.get("logic_markers", {}).get(operator, [])
            if not isinstance(basis, str) or not basis.strip() or not any(basis in q for q in quotes):
                raise ServiceError("逻辑依据必须逐字来自证据")
            if not any(marker in basis for marker in markers):
                raise ServiceError("逻辑依据未包含该运算对应的明确连接词，请核对或保留原文待审")
        if operator == "NOT" and isinstance(children[0], dict):
            leaf = children[0].get("condition", "")
            if leaf.startswith(("不", "未", "无", "禁止", "不得")):
                raise ServiceError("NOT 子项已含否定，可能造成双重否定；请保留完整原文或拆出肯定命题")
        for child in children:
            if not isinstance(child, dict):
                raise ServiceError("条件子项必须是对象")
            self.validate_rule(child, quotes, depth + 1)

    def spg_schema(self, namespace: str) -> str:
        # 全部领域类型/关系来自同一配置；系统字段不会暴露给模型自由填写。
        common = [
            "recordId",
            "entityType",
            "region",
            "aliases",
            "attrs",
            "evidence",
            "rule",
            "canonicalKey",
            "contentHash",
        ]
        types = {**self.entities, "SourceDocument": {"label": "来源文档"}, "Chunk": {"label": "原文块"}}
        lines = [f"namespace {namespace}", ""]
        for name, spec in types.items():
            lines.extend([f"{name}({spec['label']}): EntityType", "    properties:"])
            for prop in common + ["content", "metadata"]:
                lines.append(f"        {prop}({prop}): Text")
            relations = []
            for rel, rule in self.relations.items():
                if name in rule["sources"]:
                    for target in rule["targets"]:
                        relations.append((rel, rule["label"], target))
            if name in self.entities:
                relations.append(("SUPPORTED_BY", "证据", "Chunk"))
            if name == "SourceDocument":
                relations.append(("HAS_CHUNK", "包含原文", "Chunk"))
            if relations:
                lines.append("    relations:")
            for rel, label, target in relations:
                lines.append(f"        {spg_name(rel)}({label}): {target}")
                lines.append("            properties:")
                for prop in ("recordId", "evidence", "contentHash", "factId", "relationType"):
                    lines.append(f"                {prop}({prop}): Text")
            lines.append("")
        return "\n".join(lines)


def merge_extractions(parts: list[Extraction], revision_id: str, region: str) -> Extraction:
    """只在同一文档版本内合并完全相同的带证据实体，跨文档保留独立断言。"""
    entities, relations = {}, {}
    for part in parts:
        remap = {}
        for entity in part.entities:
            # 属性/规则不同即为不同断言，避免把冲突事实覆盖掉。
            key = stable_id(
                revision_id,
                region,
                entity.type,
                entity.name.strip(),
                json.dumps(entity.attributes, sort_keys=True, ensure_ascii=False),
                json.dumps(entity.rule, sort_keys=True, ensure_ascii=False),
            )
            remap[entity.id] = key
            copy = entity.model_copy(deep=True)
            copy.id = key
            if key in entities:
                current = entities[key]
                current.evidence = unique_evidence(current.evidence + copy.evidence)
                current.aliases = sorted(set(current.aliases + copy.aliases))
            else:
                entities[key] = copy
        for relation in part.relations:
            copy = relation.model_copy(deep=True)
            copy.source, copy.target = remap[relation.source], remap[relation.target]
            key = (copy.source, copy.type, copy.target)
            if key in relations:
                relations[key].evidence = unique_evidence(relations[key].evidence + copy.evidence)
            else:
                relations[key] = copy
    return Extraction(
        schema_version=max((p.schema_version for p in parts), default=1),
        entities=list(entities.values()),
        relations=list(relations.values()),
    )


def unique_evidence(items):
    return list({(e.chunk_id, e.start, e.end, e.quote): e for e in items}.values())
