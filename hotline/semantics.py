"""受限名称规范化与可解释的语义质检；不将模型判断当作审核结果。"""

import re
import unicodedata

from hotline.models import Extraction, stable_id


def canonical_name(value):
    # 只统一字形和断词空白，不扩写部门简称、不纠错、不添加“申请”等业务语义。
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"\s+", " ", value)
    return re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", value)


def step_order(value):
    match = re.fullmatch(r"[（(]?第?([一二三四五六七八九十百\d]+)[）)步、.：:]?", value.strip())
    if not match:
        return None
    text = match.group(1)
    if text.isdecimal():
        return int(text) if 0 < int(text) <= 1000 else None
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    if text in digits:
        return digits[text]
    if "百" in text:
        return None
    if text.count("十") == 1:
        first, last = text.split("十")
        if first in (*digits, "") and last in (*digits, ""):
            return digits.get(first, 1) * 10 + digits.get(last, 0)
    return None


def flow_order(basis, name):
    parts = [canonical_name(part) for part in re.split(r"\s*(?:->|→|⇒|—|－|-)\s*", basis)]
    name = canonical_name(name)
    if len(parts) >= 2 and all(parts) and parts.count(name) == 1:
        return parts.index(name) + 1
    return None


def entity_sequence(entity):
    return step_order(entity.attributes.get("order", "")) or flow_order(entity.sequence_basis, entity.name)


def is_applicant_category(text):
    return bool(
        re.search(r"法人|自然人|个人|单位|公民|组织", text)
        and re.fullmatch(r"(?:企业法人|自然人|法人|个人|单位|公民|其他组织|及|和|、|与|或|\s)+", text)
    )


def quality_report(extraction):
    extraction = Extraction.model_validate(extraction)
    issues = []

    def add(entity, code, message):
        issues.append(
            {
                "id": stable_id(entity.id, code),
                "entity_id": entity.id,
                "code": code,
                "name": entity.name,
                "message": message,
            }
        )

    for entity in extraction.entities:
        text = entity.attributes.get("content") or entity.name
        if entity.type == "ServiceItem" and re.search(r"(?:许可证|资格证|登记证)$", entity.name):
            add(
                entity,
                "SERVICE_OR_CERTIFICATE",
                "核对事项与证照名称：原文是否明确办理动作？不能自行补写申请、变更或延续。",
            )
        if entity.type == "EligibilityCondition":
            if is_applicant_category(text):
                add(
                    entity,
                    "APPLICANT_AS_CONDITION",
                    "申请主体类别被建为条件，请核对 applicants 属性，避免将主体与资格条件混淆。",
                )
            if not entity.rule:
                add(
                    entity,
                    "CONDITION_LOGIC",
                    "条件尚无结构化逻辑，请核对完整要求、否定及例外；无法确定时保留原文并说明原因。",
                )
        if entity.type == "RequiredMaterial" and re.search(r"或者|或|任一|二选一", text) and not entity.rule:
            add(entity, "MATERIAL_ALTERNATIVE", "材料含备选语义但未表示 OR，不能将各备选项当作全部必交材料。")
        if entity.type == "RequiredMaterial" and entity.rule and entity.attributes.get("format"):
            add(
                entity,
                "MATERIAL_FORMAT_SCOPE",
                "材料组同时含逻辑和形式要求，请核对原件/复印件适用哪些分支，不能自行推广到所有备选项。",
            )
        if entity.type == "ProcedureStep" and entity_sequence(entity) is None:
            add(entity, "STEP_ORDER", "步骤缺少可验证的明确序号；图中的排列不代表办理先后顺序。")
        if entity.rule:
            pending = [entity.rule]
            while pending:
                rule = pending.pop()
                if "operator" in rule:
                    if not rule.get("basis"):
                        add(
                            entity,
                            "RULE_BASIS",
                            "逻辑运算缺少原文连接词依据，请核对 AND/OR/NOT 是否由原文支持。",
                        )
                        break
                    pending.extend(rule.get("conditions", []))
        if entity.type in {"EligibilityCondition", "RequiredMaterial", "ProcedureStep", "ServiceChannel"}:
            if not any(r.target == entity.id for r in extraction.relations):
                add(entity, "UNLINKED_REQUIREMENT", "该业务要求未关联事项，客服按事项检索时可能遗漏。")
    return {
        "schema_version": extraction.schema_version,
        "issues": issues,
        "requires_acknowledgement": extraction.schema_version >= 2 and bool(issues),
        "message": "自动质检仅提示可检测问题，不能替代原文审核或证明抽取完整。",
    }
