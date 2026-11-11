"""基于已发布图事项的候选召回；名称相近和上下级地区均须用户确认。"""

import re
import unicodedata
from dataclasses import dataclass

from hotline.models import Question

REGION_END = r"省|自治区|特别行政区|自治州|地区|市(?!场)|自治县|县|区(?!域|分)|旗"
GENERIC_REGIONS = {
    "地区",
    "哪些地区",
    "什么地区",
    "这个地区",
    "本地区",
    "这个区",
    "全省",
    "本省",
    "全市",
    "本市",
    "当地",
}
GENERIC_TERMS = {
    "申请",
    "办理",
    "许可",
    "可证",
    "登记",
    "需要",
    "什么",
    "哪些",
    "条件",
    "材料",
    "事项",
    "服务",
    "指南",
    "政策",
    "满足",
    "要求",
}


def region_paths(region):
    """仅拆分来源已经标注的行政层级，不调用地理推断或自动扩写地名。"""
    parts = re.findall(rf".+?(?:{REGION_END})", region or "")
    if "".join(parts) != region:
        return {region} if region else set()
    return {
        "".join(parts[start:end]) for start in range(len(parts)) for end in range(start + 1, len(parts) + 1)
    }


def region_matches(actual, requested):
    return not requested or requested in region_paths(actual)


def question_regions(question, known_regions):
    known = {part for region in known_regions for part in region_paths(region) if part and part in question}
    # 捕捉明确但库中不存在的地区，避免“广东省”问题回落到辽宁资料。
    unknown = set()
    for match in re.finditer(
        rf"(?:^|[，。？！、\s]|请问|咨询|关于|了解|在|和|与|及)([\u4e00-\u9fff]{{2,12}}?(?:{REGION_END}))",
        question,
    ):
        value = match.group(1)
        value = re.sub(r"^(?:(?:请问|请教|您好|你好|我想|想|咨询|了解|一下|在|有关|关于|办理))+", "", value)
        if value not in GENERIC_REGIONS and len(value) >= 3 and not any(part in value for part in known):
            unknown.add(value)
    values = known | unknown
    return sorted(value for value in values if not any(value != other and value in other for other in values))


def normalize(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", value).lower())


def bigrams(value):
    value = normalize(value)
    return {value[i : i + 2] for i in range(len(value) - 1)}


def candidate_score(node, text, title):
    """词面相近只用于展示候选，永不借此自动合并实体或确认政策范围。"""
    names = [node["name"], *node.get("aliases", [])]
    clean = normalize(text)
    code = node.get("attrs", {}).get("service_code", "")
    if code and code == text.strip():
        return 100
    if any(normalize(name) and normalize(name) in clean for name in names):
        return 100
    query = bigrams(text)
    name_hits = query & set().union(*(bigrams(name) for name in names))
    title_hits = query & bigrams(title)
    if not name_hits or len(name_hits | title_hits) < 2 or not (name_hits - GENERIC_TERMS):
        return 0
    return 3 * len(name_hits) + len(title_hits)


@dataclass
class Resolution:
    request: Question
    nodes: list
    method: str
    clarification: str = ""


def resolve_services(graph, request, sources):
    hints = question_regions(request.question, [s["region"] or "" for s in sources.values()])
    if len(hints) > 1:
        return Resolution(
            request,
            [],
            "MULTIPLE_REGIONS",
            "问题涉及多个地区，请分别指定具体办理地区查询，避免混合不同地区的要求。",
        )
    region = request.region.strip()
    conflict = False
    if hints:
        hint = hints[0]
        if region and not region_matches(region, hint) and not region_matches(hint, region):
            conflict = True
        if not region or not region_matches(region, hint):
            region = hint
    resolved = request.model_copy(update={"region": region, "service": request.service.strip()})
    # 来源元数据与图节点的适用地区必须一致；宽泛地区仅用于候选召回。
    scoped = {rid: s for rid, s in sources.items() if region_matches(s["region"] or "", region)}
    if not scoped:
        return Resolution(resolved, [], "NO_REGION_MATCH")
    nodes, overflow = graph.services(list(scoped), request.question, region, resolved.service)
    nodes = [
        n
        for n in nodes
        if n.get("recordId") in scoped and n.get("region", "") == (scoped[n["recordId"]]["region"] or "")
    ]
    if request.service_code:
        nodes = [n for n in nodes if n.get("attrs", {}).get("service_code") == request.service_code]
    if overflow:
        return Resolution(resolved, [], "LIMIT", "候选事项过多，请填写完整事项名称和具体地区。")
    if nodes and resolved.service:
        mentioned, many = graph.services(list(scoped), request.question, region, "")
        selected_names = {name for n in nodes for name in [n["name"], *n.get("aliases", [])]}
        other = [
            n
            for n in mentioned
            if n.get("recordId") in scoped
            and n["name"] not in selected_names
            and n.get("region", "") == (scoped[n["recordId"]]["region"] or "")
        ]
        if other or many:
            return Resolution(
                resolved,
                other[:10],
                "SCOPE_CONFLICT",
                "问题中的事项与当前查询范围不一致，请确认具体事项后查询。",
            )
    if nodes and not conflict:
        return Resolution(resolved, nodes, "EXACT")
    candidates, overflow = graph.service_candidates(list(scoped))
    if overflow:
        return Resolution(resolved, [], "LIMIT", "相关图事项超过候选检索上限，请填写完整事项名称和具体地区。")
    scored = []
    search = resolved.service or request.question
    for node in candidates:
        if request.service_code and node.get("attrs", {}).get("service_code") != request.service_code:
            continue
        source = scoped.get(node.get("recordId"))
        if not source or node.get("region", "") != (source["region"] or ""):
            continue
        score = candidate_score(node, search, source["title"] or source["filename"])
        if score:
            scored.append((score, node))
    scored.sort(key=lambda item: (-item[0], item[1]["name"], item[1].get("region", ""), item[1]["recordId"]))
    candidates = [node for _, node in scored[:10]]
    if conflict:
        return Resolution(
            resolved,
            candidates,
            "SCOPE_CONFLICT",
            "问题中的地区与当前查询范围不一致。请确认下方地区与事项后重新查询。",
        )
    if candidates:
        areas = sorted({n.get("region", "") for n in candidates})
        message = "找到名称相关的已发布事项，请确认具体事项和办理地区后查询。"
        if region and region not in areas:
            message = f"找到相关的已发布指南，但其适用地区为{'、'.join(areas)}，不能直接作为{region}的统一规定。请选择具体办理地区继续查询。"
        if len(scored) > 10:
            message += " 当前仅展示前 10 个候选，可补充名称或地区缩小范围。"
        return Resolution(resolved, candidates, "CANDIDATES", message)
    return Resolution(resolved, [], "NO_SERVICE_MATCH")
