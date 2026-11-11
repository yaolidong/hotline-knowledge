"""将 backup_hotline_data 产出恢复到隔离目录；不覆盖现网 HOTLINE_DATA_DIR。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True, help="含 BACKUP_META.json 的备份目录")
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="隔离恢复目录（须为空或不存在；禁止写回生产 data）",
    )
    args = parser.parse_args(argv)
    backup = args.backup.resolve()
    target = args.target.resolve()
    meta_path = backup / "BACKUP_META.json"
    if not meta_path.exists():
        raise SystemExit(f"不是有效备份目录：缺少 {meta_path}")
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"目标非空，拒绝覆盖：{target}")
    if target.exists():
        target.rmdir()
    shutil.copytree(
        backup,
        target,
        ignore=shutil.ignore_patterns("BACKUP_META.json"),
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    note = {
        "restored_from": str(backup),
        "target": str(target),
        "original_source": meta.get("source"),
        "next": "设置 HOTLINE_DATA_DIR 指向该隔离目录后运行 hotline doctor；Neo4j/MySQL 须另行恢复对应卷快照。",
    }
    (target / "RESTORE_META.json").write_text(json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(note, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
