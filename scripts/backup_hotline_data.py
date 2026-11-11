"""备份 HOTLINE_DATA_DIR 到带时间戳目录；不触碰 Neo4j/MySQL 卷（需按 deploy README 另行快照）。"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from hotline.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path("data/backups"))
    args = parser.parse_args(argv)
    settings = Settings()
    source = Path(settings.data_dir).resolve()
    if not source.exists():
        raise SystemExit(f"数据目录不存在：{source}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (args.output_root / f"hotline-data-{stamp}").resolve()
    if target.exists():
        raise SystemExit(f"目标已存在：{target}")
    args.output_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("backups", "config-backups", "__pycache__", "*.pyc"),
    )
    meta = {
        "source": str(source),
        "target": str(target),
        "created": stamp,
        "note": "仅应用数据目录；请同时备份 Neo4j/MySQL 命名卷后再称可恢复。",
    }
    (target / "BACKUP_META.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
