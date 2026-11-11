"""事项清单 CSV 适配器：保存原件，每行独立抽取，编码和范围采用明确的列映射。"""

import csv
import io

from hotline.models import Chunk, Entity, Evidence, Extraction, Relation, stable_id
from hotline.parsing import Parsed, decode_text
from hotline.schema import merge_extractions
from hotline.settings import ServiceError

FIELDS = {
    "service_name": "事项名称",
    "service_code": "事项编码",
    "region": "适用地区",
    "department": "办理部门",
    "fees": "收费标准",
    "time_limit": "办理时限",
    "applicants": "适用对象",
}
REQUIRED = {"service_name", "service_code", "region"}


def read_directory(data):
    try:
        reader = csv.reader(io.StringIO(decode_text(data)), strict=True)
        headers = next(reader)
        if not 3 <= len(headers) <= 40 or any(not h.strip() or len(h) > 100 for h in headers):
            raise ServiceError("目录需要 3 至 40 个非空列名，列名不超过 100 字")
        if len(set(headers)) != len(headers):
            raise ServiceError("目录包含重复列名，无法准确映射")
        rows = []
        for values in reader:
            if not values or not any(v.strip() for v in values):
                continue
            if len(values) != len(headers):
                raise ServiceError(f"目录第 {reader.line_num} 行列数与表头不一致")
            if len(rows) >= 50 or sum(len(v) for v in values) > 10000:
                raise ServiceError("每批最多 50 条事项，每条原始字段合计不超过 10000 字，请分批导入")
            rows.append({"values": values, "line_end": reader.line_num})
        if not rows:
            raise ServiceError("目录没有数据行")
        return headers, rows
    except (csv.Error, StopIteration) as exc:
        raise ServiceError("CSV 格式不正确，请使用带表头的逗号分隔 UTF-8 或 GB18030 文件") from exc


def preview(data):
    headers, rows = read_directory(data)
    return {
        "headers": headers,
        "fields": FIELDS,
        "required": sorted(REQUIRED),
        "suggested_mapping": {k: v for k, v in FIELDS.items() if v in headers},
        "sample": [row["values"] for row in rows[:5]],
        "row_count": len(rows),
        "message": "请确认字段含义。一个文件只接收同一实施地区；同名不同事项须使用不同编码。",
    }


def parse_directory(data, mapping, region, revision_id, max_chars, max_chunks):
    headers, rows = read_directory(data)
    if (
        not isinstance(mapping, dict)
        or not REQUIRED.issubset(mapping)
        or not set(mapping).issubset(FIELDS)
        or any(not isinstance(v, str) or v not in headers for v in mapping.values())
        or len(set(mapping.values())) != len(mapping)
    ):
        raise ServiceError("字段映射不合法：必须指定不同列的事项名称、事项编码与适用地区")
    if not region.strip() or len(rows) > max_chunks:
        raise ServiceError("请指定本批实施地区，且记录数不能超过文本块上限")
    chunks, cells, tables, row_metadata = [], [], [], []
    text, codes = "", set()
    for index, row in enumerate(rows, 1):
        values = dict(zip(headers, row["values"]))
        fields = {
            key: values[column].replace("\r\n", "\n").replace("\r", "\n").strip()
            for key, column in mapping.items()
        }
        if any(not fields[key] for key in REQUIRED):
            raise ServiceError(f"第 {index} 条事项缺少名称、编码或地区")
        if fields["region"] != region.strip():
            raise ServiceError(f"第 {index} 条事项地区与本批地区不同，请按地区拆分，避免混用要求")
        if fields["service_code"] in codes:
            raise ServiceError(f"第 {index} 条事项编码重复，请先核对实施事项唯一性")
        codes.add(fields["service_code"])
        if len(fields["service_name"]) > 300 or len(fields["service_code"]) > 100:
            raise ServiceError(f"第 {index} 条事项名称或编码过长")
        start = len(text)
        body = f"目录记录 {index}（原 CSV 截至第 {row['line_end']} 行）\n"
        for column, (header, value) in enumerate(values.items(), 1):
            body += header + "："
            left = start + len(body)
            body += value.replace("\r\n", "\n").replace("\r", "\n")
            cells.append(
                {
                    "table": index,
                    "row": index + 1,
                    "column": column,
                    "header": header,
                    "text": body[left - start :],
                    "start": left,
                    "end": start + len(body),
                    "csv_line_end": row["line_end"],
                }
            )
            body += "\n"
        chunk = Chunk(
            id=stable_id(revision_id, str(start)),
            text=body,
            start=start,
            end=start + len(body),
            section=f"目录记录 {index} · {fields['service_code']}",
        )
        if len(body) > 12000:
            raise ServiceError(f"第 {index} 条记录连同表头超过证据长度上限，请缩减列名或拆分资料")
        chunks.append(chunk)
        tables.append({"id": index, "start": start, "end": chunk.end})
        row_metadata.append({"chunk_id": chunk.id, "fields": fields, "row": index + 1})
        text += body + "\n"
    if len(text) > max_chars:
        raise ServiceError("目录解析文本长度超限")
    # 不为独立事项建立 previous/following，避免跨行上下文污染。
    return Parsed(
        text,
        chunks,
        {
            "_source": {
                "version": 3,
                "pages": [
                    {
                        "page": None,
                        "method": "csv",
                        "start": 0,
                        "end": len(text),
                        "lines": [],
                        "cells": cells,
                        "tables": tables,
                        "warnings": [],
                    }
                ],
            },
            "directory": {"version": 1, "mapping": mapping, "rows": row_metadata},
        },
    )


def extract_directory(parsed, extractor, schema, metadata, revision_id, progress):
    parts = []
    for index, (chunk, row) in enumerate(zip(parsed.chunks, parsed.metadata["directory"]["rows"])):
        fields = row["fields"]
        # 复用现有模型、Schema 校验与证据链；每次模型只看到一条实施事项。
        part = extractor.extract([chunk], metadata, stable_id(revision_id, chunk.id), lambda *_: None)
        services = [e for e in part.entities if e.type == "ServiceItem"]
        if any(e.name.strip() != fields["service_name"] for e in services) or len(services) > 1:
            raise ServiceError(f"目录第 {index + 1} 条抽取了不一致的事项身份，请检查原文或字段映射")
        evidence = Evidence(chunk_id=chunk.id, quote=chunk.text, start=0, end=len(chunk.text))
        if services:
            service = services[0]
            service.evidence = [evidence]
        else:
            service = Entity(
                id=stable_id(chunk.id, "service"),
                type="ServiceItem",
                name=fields["service_name"],
                evidence=[evidence],
            )
            part.entities.append(service)
        for key in ("service_code", "fees", "time_limit", "applicants"):
            if fields.get(key):
                if service.attributes.get(key) and service.attributes[key] != fields[key]:
                    raise ServiceError(f"目录第 {index + 1} 条的 {FIELDS[key]} 与抽取值不一致，未覆盖原值")
                service.attributes[key] = fields[key]
        for field, kind, relation in (
            ("region", "AdministrativeRegion", "APPLIES_TO_REGION"),
            ("department", "GovernmentDepartment", "HANDLED_BY"),
        ):
            if not fields.get(field):
                continue
            name = fields[field]
            target = next((e for e in part.entities if e.type == kind and e.name == name), None)
            if target is None:
                target = Entity(id=stable_id(chunk.id, field), type=kind, name=name, evidence=[evidence])
                part.entities.append(target)
            if not any(
                r.source == service.id and r.target == target.id and r.type == relation
                for r in part.relations
            ):
                part.relations.append(
                    Relation(source=service.id, target=target.id, type=relation, evidence=[evidence])
                )
        parts.append(schema.validate(part, [chunk]))
        progress(index + 1, len(parsed.chunks))
    merged = merge_extractions(parts, revision_id, metadata["region"])
    merged.schema_version = 2
    return schema.validate(Extraction.model_validate(merged.model_dump()), parsed.chunks)
