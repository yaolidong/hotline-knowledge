"""仅处理有明确语法证据的建模规范化；保留逐字事实及转换记录，疑义交给审核。"""

import re

from hotline.models import Evidence
from hotline.semantics import is_applicant_category


def normalize_extraction(extraction, chunks):
    result = extraction.model_copy(deep=True)
    result.normalizations = []
    entities = {e.id: e for e in result.entities}
    removed = set()
    for entity in result.entities:
        incoming = [r for r in result.relations if r.target == entity.id]
        # 只重新归类纯主体名词，不触碰含资格限制或其他关系的条件。
        if (
            entity.type == "EligibilityCondition"
            and is_applicant_category(entity.name)
            and not entity.rule
            and set(entity.attributes) <= {"content"}
            and entity.attributes.get("content", entity.name) == entity.name
            and len(incoming) == 1
            and incoming[0].type == "REQUIRES_CONDITION"
            and not any(r.source == entity.id for r in result.relations)
        ):
            service = entities[incoming[0].source]
            if service.type == "ServiceItem" and not service.attributes.get("applicant_scope"):
                service.attributes["applicant_scope"] = entity.name
                service.evidence.extend(entity.evidence)
                removed.add(entity.id)
                result.normalizations.append(
                    {
                        "kind": "APPLICANT_CATEGORY",
                        "entity_id": entity.id,
                        "target_id": service.id,
                        "original_name": entity.name,
                        "field": "applicant_scope",
                        "method": "literal-category-v1",
                    }
                )
        if entity.type == "RequiredMaterial" and entity.rule is None:
            parts = entity.name.split("或者")
            # 仅接受两项简单备选，带其他并列、条件、例外或标点的句子留待审核。
            if (
                len(parts) == 2
                and all(len(p.strip()) >= 2 for p in parts)
                and not re.search(
                    r"[，,；;。]|以及|并且|同时|除|如果|应当|需要|提交|提供|或|且|与|和", "".join(parts)
                )
            ):
                entity.rule = {
                    "operator": "OR",
                    "basis": entity.name,
                    "conditions": [{"condition": p.strip()} for p in parts],
                }
                result.normalizations.append(
                    {
                        "kind": "MATERIAL_OR",
                        "entity_id": entity.id,
                        "original_name": entity.name,
                        "method": "literal-alternative-v1",
                    }
                )
    result.entities = [e for e in result.entities if e.id not in removed]
    result.relations = [r for r in result.relations if r.source not in removed and r.target not in removed]
    steps = [e for e in result.entities if e.type == "ProcedureStep" and not e.sequence_basis]
    # 对“办理流程：甲-乙-丙”这样的明示线性链定位，不从段落排列或自然语言推断顺序。
    names = sorted({re.escape(e.name) for e in steps}, key=len, reverse=True)
    if 2 <= len(names) <= 100:
        name = "(?:" + "|".join(names) + ")"
        pattern = re.compile(
            r"(?:办理流程|办理顺序)[：:\s]*(?P<chain>"
            + name
            + r"(?:\s*(?:->|→|⇒|－|-)\s*"
            + name
            + r"){1,99})"
        )
        for chunk in chunks:
            for match in pattern.finditer(chunk.text):
                chain = match.group("chain")
                pieces = re.split(r"\s*(?:->|→|⇒|－|-)\s*", chain)
                if len(set(pieces)) != len(pieces):
                    continue
                linked = []
                for step in steps:
                    if step.name in pieces and not step.sequence_basis:
                        step.sequence_basis = chain
                        step.evidence.append(
                            Evidence(
                                chunk_id=chunk.id, quote=match.group(), start=match.start(), end=match.end()
                            )
                        )
                        linked.append(step.id)
                if linked:
                    result.normalizations.append(
                        {
                            "kind": "EXPLICIT_FLOW",
                            "entity_ids": linked,
                            "basis": chain,
                            "method": "literal-flow-v1",
                        }
                    )
    # 相同引文只保留一份，不丢失任何不同来源。
    for entity in result.entities:
        entity.evidence = list({(e.chunk_id, e.start, e.end, e.quote): e for e in entity.evidence}.values())
    return result
