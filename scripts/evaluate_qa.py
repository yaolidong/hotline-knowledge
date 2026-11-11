"""比较问答实际结果与金标准问题集。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hotline.gold import evaluate_qa_pack, load_pack, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("actual", type=Path, help="实际问答结果 JSON 列表或 {answers:[...]}")
    parser.add_argument("gold", type=Path, help="金标准 pack.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.actual.read_text(encoding="utf-8"))
    answers = payload if isinstance(payload, list) else payload.get("answers") or []
    gold = load_pack(args.gold)
    report = evaluate_qa_pack(answers, gold)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        write_report(args.output, report)
    return 0 if report.get("metrics") is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
