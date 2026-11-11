"""按锁定提交下载源码；已有目录只验证，不覆盖。"""

import io
import json
import subprocess
import urllib.request
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
lock = json.loads((root / "upstream.lock.json").read_text(encoding="utf-8"))
for name, entry in lock.items():
    destination = root / "vendor" / name
    if destination.exists():
        if (destination / ".git").exists():
            current = subprocess.check_output(
                ["git", "-C", str(destination), "rev-parse", "HEAD"], text=True
            ).strip()
        else:
            current = (destination / ".upstream-commit").read_text().strip()
        if current != entry["commit"]:
            raise SystemExit(f"{name} 提交不匹配；保留现有文件，请使用新的工作目录获取。")
        print(f"{name}: 已存在且提交匹配")
        continue
    repo = entry["url"].removeprefix("https://github.com/").removesuffix(".git")
    url = f"https://codeload.github.com/{repo}/zip/{entry['commit']}"
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    destination.mkdir(parents=True)
    for info in archive.infolist():
        relative = Path(*Path(info.filename).parts[1:])
        target = (destination / relative).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise SystemExit("压缩包包含不安全路径")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(archive.read(info))
    (destination / ".upstream-commit").write_text(entry["commit"])
    print(f"{name}: 已获取固定提交 {entry['commit']}")
