"""配置现有本地嵌入服务；不改系统环境变量，不打印令牌，不覆盖外部服务设置。"""

import re
import shutil
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    path = Path(".env")
    values = dotenv_values(path)
    existing = values.get("HOTLINE_EMBEDDING_BASE_URL")
    if existing and existing != "http://127.0.0.1:8090/v1":
        raise SystemExit("已有其他嵌入服务配置，未覆盖。")
    credentials = dotenv_values("deploy/images.env")
    key = credentials.get("EMBEDDING_API_KEY", "")
    if len(key) < 24:
        raise SystemExit("缺少现有本地嵌入服务凭据。")
    with httpx.Client(trust_env=False, timeout=90) as client:
        response = client.post(
            "http://127.0.0.1:8090/v1/embeddings",
            headers={"Authorization": "Bearer " + key},
            json={"model": "BAAI/bge-small-zh-v1.5", "input": "合成测试：办理申请材料"},
        )
        if response.status_code != 200:
            raise SystemExit(f"本地嵌入服务验证失败：HTTP {response.status_code}；未修改配置。")
        dimension = len(response.json()["data"][0]["embedding"])
    backup = Path("data/config-backups")
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup / ("env-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    content = path.read_text(encoding="utf-8")
    for name, value in {
        "HOTLINE_EMBEDDING_BASE_URL": "http://127.0.0.1:8090/v1",
        "HOTLINE_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
        "HOTLINE_EMBEDDING_API_KEY": key,
    }.items():
        line = f"{name}={value}"
        content = (
            re.sub(rf"^{name}=.*$", lambda _: line, content, flags=re.MULTILINE)
            if re.search(rf"^{name}=", content, flags=re.MULTILINE)
            else content.rstrip() + "\n" + line + "\n"
        )
    path.write_text(content, encoding="utf-8")
    print(f"本地配置完成，真实嵌入维度 {dimension}；原配置已在忽略目录备份。")


if __name__ == "__main__":
    main()
