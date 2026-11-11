"""把经过校验的文本范围映射到已保存的版面信息，不使用文本相似性猜测位置。"""

from hotline.models import stable_id
from hotline.settings import ServiceError


def locate_evidence(record, chunk, start, end):
    if not 0 <= start < end <= len(chunk["text"]) or end - start > 12000:
        raise ServiceError("证据范围不合法")
    lo, hi = chunk["start"] + start, chunk["start"] + end
    regions = []
    for page in (record.get("parsed_metadata") or {}).get("_source", {}).get("pages", []):
        base = page.get("start", 0)
        for kind, items in (("line", page.get("lines", [])), ("cell", page.get("cells", []))):
            for item in items:
                left, right = base + item["start"], base + item["end"]
                if left < hi and right > lo:
                    regions.append(
                        {
                            **item,
                            "kind": kind,
                            "page": page.get("page"),
                            "method": page["method"],
                            "absolute_start": left,
                            "absolute_end": right,
                            "id": stable_id(record["id"], str(left), str(right), kind),
                        }
                    )
    return {
        "revision_id": record["id"],
        "document_id": record["document_id"],
        "source_hash": record["sha256"],
        "version": record["number"],
        "chunk_id": chunk["id"],
        "start": start,
        "end": end,
        "quote": chunk["text"][start:end],
        "page": chunk["page"],
        "regions": regions,
        "status": "LOCATED" if regions else "TEXT_ONLY",
        "coordinate_system": "normalized_rendered_page_top_left",
        "parser_version": (record.get("parsed_metadata") or {}).get("_source", {}).get("version", 1),
    }
