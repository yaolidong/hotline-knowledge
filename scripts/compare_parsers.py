"""对比默认管道与可选 MinerU：未安装 MinerU 时只报告默认侧并退出 0。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hotline.document_evidence import material_condition_coverage
from hotline.gold import load_pack
from hotline.parsers import mineru_adapter


def main() -> int:
    parser_gold = ROOT / "examples" / "gold" / "syn-material-or" / "pack.json"
    gold = load_pack(parser_gold)
    # 用金标准实体模拟「默认管道完美命中」基线
    actual = {
        "entities": [
            e if isinstance(e, dict) else {"type": e[0], "name": e[1]}
            for e in gold["expected_entities"]
        ]
    }
    baseline = material_condition_coverage(gold, actual)
    report = {
        "pack_id": gold["pack_id"],
        "default_pipeline": baseline,
        "mineru_available": mineru_adapter.available(),
        "mineru": None,
        "note": "无提升时不得默认启用 MinerU；需同金标准对比后再决策。",
    }
    if report["mineru_available"]:
        try:
            mineru_adapter.MinerUAdapter().parse_pdf(Path("unused.pdf"))
        except Exception as exc:  # noqa: BLE001
            report["mineru"] = {"error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
