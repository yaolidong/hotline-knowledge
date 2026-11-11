"""用真实本地嵌入服务创建隔离的 OpenSPG 项目，并保存实际项目 ID/数据库名称。"""

import json
import re
from pathlib import Path

import httpx
from dotenv import dotenv_values

from hotline.settings import Settings


def main():
    settings = Settings()
    if not settings.namespace.startswith("HotlineTest"):
        raise SystemExit("本脚本只允许 HotlineTest 测试命名空间。")
    credentials = dotenv_values("deploy/images.env")
    with httpx.Client(base_url=settings.openspg_url, timeout=180, trust_env=False) as client:
        try:
            response = client.get("/public/v1/project")
        except httpx.ConnectError as exc:
            raise SystemExit(
                f"无法连接 OpenSPG（{settings.openspg_url}）：{exc}。"
                "请先按 deploy/README.md 第 6 节启动依赖并等待 8887 就绪。"
            ) from exc
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise SystemExit("无法读取 OpenSPG 项目列表。")
        projects = [p for p in body if p["namespace"] == settings.namespace]
        if len(projects) > 1:
            raise SystemExit("命名空间对应多个项目，未进行修改。")
        if projects:
            project = projects[0]
            print("复用已有测试项目，未修改项目配置。")
        else:
            config = {
                "vectorizer": {
                    "type": "openai",
                    "model": "BAAI/bge-small-zh-v1.5",
                    "base_url": "http://embedding:8090/v1",
                    "api_key": credentials["EMBEDDING_API_KEY"],
                }
            }
            response = client.post(
                "/public/v1/project",
                json={
                    "name": "政务热线合成验收",
                    "namespace": settings.namespace,
                    "config": config,
                    "autoSchema": False,
                    "visibility": "PRIVATE",
                    "tag": "LOCAL",
                    "userNo": "hotlinetest",
                },
            )
            try:
                body = response.json()
            except ValueError:
                body = {"error": response.text}
            if response.status_code != 200 or not isinstance(body, dict) or not body.get("id"):
                # 上游错误可能包含配置密钥；仅在本地诊断时按密钥脱敏后查看。
                diagnostic = json.dumps(body, ensure_ascii=False, indent=2)
                for value in credentials.values():
                    if value:
                        diagnostic = diagnostic.replace(value, "[REDACTED]")
                Path("data/validation/project-error.json").write_text(diagnostic, encoding="utf-8")
                raise SystemExit("创建项目失败，脱敏诊断已保存 data/validation/project-error.json。")
            project = body
        config = project["config"]
        if isinstance(config, str):
            config = json.loads(config)
        database = config["graph_store"]["database"]
        env = Path(".env")
        content = env.read_text(encoding="utf-8")
        for key, value in {
            "HOTLINE_OPENSPG_PROJECT_ID": str(project["id"]),
            "HOTLINE_NEO4J_DATABASE": database,
        }.items():
            content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
        env.write_text(content, encoding="utf-8")
        print(
            json.dumps(
                {"project_id": project["id"], "namespace": settings.namespace, "database": database},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
