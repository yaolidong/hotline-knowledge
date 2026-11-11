"""模型传输层的原文片段引用；入库前仍执行完整逐字证据校验。"""

import copy
import re

from hotline.settings import ServiceError


def evidence_spans(chunks):
    spans = {}
    for chunk in chunks:
        # 完整条款用于跨句条件；较长块另提供段落引用，避免模型手工计算偏移。
        ranges = [(0, len(chunk.text))]
        if len(chunk.text) > 800:
            ranges += [(m.start(), m.end()) for m in re.finditer(r"[^\n]+(?:\n|$)", chunk.text)]
        for start, end in dict.fromkeys(ranges):
            if chunk.text[start:end].strip():
                spans[f"{chunk.id}:{start}:{end}"] = {
                    "chunk_id": chunk.id,
                    "quote": chunk.text[start:end],
                    "start": start,
                    "end": end,
                }
    return spans


def transport_schema(schema):
    schema = copy.deepcopy(schema)
    evidence = schema["$defs"]["Evidence"]
    schema["$defs"]["Evidence"] = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"span_id": {"type": "string"}},
                "required": ["span_id"],
                "additionalProperties": False,
            },
            evidence,
        ]
    }
    return schema


def exact_layout_match(value, source):
    """只恢复空白排版，必须唯一；不做模糊匹配、标点替换或 OCR 纠错。"""
    if not isinstance(value, str) or not value.strip():
        return None
    positions = [i for i, char in enumerate(source) if not char.isspace()]
    compact = "".join(source[i] for i in positions)
    needle = "".join(value.split())
    start = compact.find(needle)
    if start < 0 or compact.find(needle, start + 1) >= 0:
        return None
    left, right = positions[start], positions[start + len(needle) - 1] + 1
    return left, right, source[left:right]


def coalesce_evidence(references, sources):
    """只合并同块中已核验、相接或重叠的范围，不跨过未引用的文字。"""
    valid, other = [], []
    for e in references:
        source, start, end = sources.get(e.get("chunk_id")), e.get("start"), e.get("end")
        if (
            set(e) <= {"chunk_id", "quote", "start", "end"}
            and source is not None
            and isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(source)
            and source[start:end] == e.get("quote")
        ):
            valid.append(e)
        else:
            other.append(e)
    merged = []
    for e in sorted(valid, key=lambda item: (item["chunk_id"], item["start"], item["end"])):
        if merged and merged[-1]["chunk_id"] == e["chunk_id"] and e["start"] <= merged[-1]["end"]:
            previous = merged[-1]
            previous["end"] = max(previous["end"], e["end"])
            previous["quote"] = sources[e["chunk_id"]][previous["start"] : previous["end"]]
        else:
            merged.append(dict(e))
    return merged + other


def bind_evidence(payload, chunks):
    payload = copy.deepcopy(payload)
    if not isinstance(payload, dict):
        raise ServiceError("模型返回不符合结构要求的 JSON")
    spans, sources = evidence_spans(chunks), {c.id: c.text for c in chunks}
    changes = []
    for group in ("entities", "relations"):
        items = payload.get(group, [])
        if not isinstance(items, list):
            raise ServiceError("模型实体或关系必须是列表")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("evidence", []), list):
                raise ServiceError("模型事实或证据结构不合法")
            references = item.get("evidence", [])
            for i, evidence in enumerate(references):
                if not isinstance(evidence, dict):
                    raise ServiceError("模型证据必须是对象")
                if "span_id" in evidence:
                    if set(evidence) != {"span_id"} or evidence["span_id"] not in spans:
                        raise ServiceError("证据片段编号不存在或包含额外字段")
                    references[i] = dict(spans[evidence["span_id"]])
                    continue
                quote, source = evidence.get("quote"), sources.get(evidence.get("chunk_id"))
                if not isinstance(quote, str) or source is None:
                    continue
                if quote and source.count(quote) == 1:
                    evidence.update(start=source.index(quote), end=source.index(quote) + len(quote))
                elif quote not in source:
                    match = exact_layout_match(quote, source)
                    if match:
                        start, end, original = match
                        evidence.update(quote=original, start=start, end=end)
                        changes.append(
                            {
                                "kind": "EVIDENCE_WHITESPACE",
                                "item": f"{group}:{index}",
                                "before": quote,
                                "after": original,
                            }
                        )
            references = coalesce_evidence(references, sources)
            item["evidence"] = references
            if group != "entities":
                continue
            quotes = [e.get("quote", "") for e in references if isinstance(e.get("quote", ""), str)]

            def restore(value, field):
                if not isinstance(value, str) or any(value in q for q in quotes):
                    return value
                matches = {m[2] for q in quotes if (m := exact_layout_match(value, q))}
                if len(matches) == 1:
                    original = matches.pop()
                    changes.append(
                        {
                            "kind": "FIELD_WHITESPACE",
                            "item": item.get("id", ""),
                            "field": field,
                            "before": value,
                            "after": original,
                        }
                    )
                    return original
                return value

            item["name"] = restore(item.get("name"), "name")
            if isinstance(item.get("attributes"), dict):
                item["attributes"] = {k: restore(v, k) for k, v in item["attributes"].items()}
            if isinstance(item.get("aliases"), list):
                item["aliases"] = [restore(v, "alias") for v in item["aliases"]]
            if item.get("sequence_basis"):
                item["sequence_basis"] = restore(item["sequence_basis"], "sequence_basis")

            def restore_rule(rule, depth=0):
                if not isinstance(rule, dict) or depth > 10:
                    return
                for key in ("condition", "basis"):
                    if key in rule:
                        rule[key] = restore(rule[key], f"rule.{key}")
                if isinstance(rule.get("conditions"), list):
                    for child in rule["conditions"]:
                        restore_rule(child, depth + 1)

            restore_rule(item.get("rule"))
    # 模型不能伪造系统规范化日志。
    payload["normalizations"] = changes
    return payload
