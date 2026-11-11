"""原生 PDF 有边框表格：保留单元格、合并跨度和页面坐标，不猜测扫描表格。"""


def extract_table_page(page):
    # 裁剪/旋转坐标尚未统一时沿用原解析器，不输出错误的定位框。
    if page.rotation or page.bbox[:2] != (0, 0) or (page.cropbox and page.cropbox != page.bbox):
        return None
    # 单行双列表常用于“办理条件 | 长段正文”，不能按行数丢弃。
    found = [table for table in page.find_tables() if len(table.columns) > 1]
    if not found:
        return None

    def box(rect):
        left, top, right, bottom = rect
        return [
            [left / page.width, top / page.height],
            [right / page.width, top / page.height],
            [right / page.width, bottom / page.height],
            [left / page.width, bottom / page.height],
        ]

    def outside(obj):
        if obj.get("object_type") != "char":
            return True
        x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
        return not any(t.bbox[0] <= x < t.bbox[2] and t.bbox[1] <= y < t.bbox[3] for t in found)

    blocks = [(line["top"], line["x0"], "line", line) for line in page.filter(outside).extract_text_lines()]
    blocks += [(t.bbox[1], t.bbox[0], "table", t) for t in found]
    text, cells, tables, lines = "", [], [], []
    for _, _, kind, item in sorted(blocks, key=lambda value: value[:2]):
        if kind == "line":
            value = item["text"]
            lines.append(
                {
                    "text": value,
                    "start": len(text),
                    "end": len(text) + len(value),
                    "box": box((item["x0"], item["top"], item["x1"], item["bottom"])),
                    "confidence": None,
                }
            )
            text += value + "\n"
            continue
        table_id, start = len(tables) + 1, len(text)
        text += "[表格]\n"
        values = item.extract()
        xs = sorted({c[0] for c in item.cells} | {c[2] for c in item.cells})
        ys = sorted({c[1] for c in item.cells} | {c[3] for c in item.cells})
        for row_index, row in enumerate(item.rows):
            for column_index, rect in enumerate(row.cells):
                if rect is not None:
                    value = values[row_index][column_index] or ""
                    cell = {
                        "table": table_id,
                        "row": row_index + 1,
                        "column": column_index + 1,
                        "row_span": max(1, ys.index(rect[3]) - ys.index(rect[1])),
                        "column_span": max(1, xs.index(rect[2]) - xs.index(rect[0])),
                        "text": value,
                        "start": len(text),
                        "end": len(text) + len(value),
                        "box": box(rect),
                    }
                    cells.append(cell)
                    if value:
                        lines.append({**cell, "confidence": None})
                    text += value
                text += " | " if column_index < len(row.cells) - 1 else "\n"
        text += "[/表格]\n"
        tables.append({"id": table_id, "start": start, "end": len(text), "box": box(item.bbox)})
    return text, {
        "method": "native",
        "parser": "pdfplumber-table",
        "cells": cells,
        "tables": tables,
        "lines": lines,
        "warnings": [],
    }
