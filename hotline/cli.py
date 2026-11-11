import argparse
import importlib.metadata
import json
import secrets
from pathlib import Path

from hotline.schema import DomainSchema
from hotline.settings import Settings


def build_doctor_report(settings: Settings) -> dict:
    import httpx

    from hotline.graph import GraphStore
    from hotline.local_llm import probe_local_llm

    report = {
        "users_configured": bool(settings.users),
        "model_configured": bool(settings.llm_base_url and settings.llm_model),
        "project_id": settings.openspg_project_id,
        "namespace": settings.namespace,
    }
    try:
        report["kag_version"] = importlib.metadata.version("openspg-kag")
    except importlib.metadata.PackageNotFoundError:
        report["kag_version"] = "未安装"
    graph = GraphStore(settings)
    try:
        graph.health()
        report["neo4j"] = "连接成功"
    except Exception:
        report["neo4j"] = "未配置或无法连接"
    finally:
        graph.close()
    try:
        response = httpx.get(settings.openspg_url, timeout=5, trust_env=settings.http_trust_env)
        report["openspg_http"] = response.status_code
    except Exception:
        report["openspg_http"] = "无法连接"
    if not settings.llm_base_url or not settings.llm_model:
        report["local_llm"] = {"status": "未配置", "detail": "缺少模型 URL 或名称"}
    else:
        report["local_llm"] = probe_local_llm(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=settings.llm_timeout,
            trust_env=settings.http_trust_env,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description="政务热线开发与部署工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建本地 .env 与四个角色的随机令牌，不覆盖已有文件")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    schema_command = commands.add_parser("schema", help="从领域配置生成并离线校验 OpenSPG Schema")
    schema_command.add_argument("--output", default="data/Hotline.schema")
    schema_command.add_argument("--apply", action="store_true", help="显式创建到空的专用项目，不修改已有类型")
    commands.add_parser("doctor", help="检查配置和真实服务连接，不输出密钥")
    args = parser.parse_args()
    if args.command == "init":
        path = Path(".env")
        if path.exists():
            parser.exit(1, ".env 已存在，未覆盖。\n")
        users = {
            secrets.token_urlsafe(32): {"name": role, "role": role}
            for role in ("admin", "maintainer", "reviewer", "agent")
        }
        example = Path(".env.example").read_text(encoding="utf-8")
        example = example.replace("HOTLINE_USERS={}", "HOTLINE_USERS='" + json.dumps(users) + "'")
        with path.open("x", encoding="utf-8") as output:
            output.write(example)
        print("已创建 .env。四个角色的令牌仅写入该文件；请在本地查看并妥善保管。")
    elif args.command == "serve":
        import uvicorn

        uvicorn.run(
            "hotline.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
        )
    elif args.command == "schema":
        from knext.schema.marklang.schema_ml import SPGSchemaMarkLang

        settings = Settings()
        content = DomainSchema().spg_schema(settings.namespace)
        parsed = SPGSchemaMarkLang("", with_server=False, script_data_str=content)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(f"Schema 离线校验通过：{len(parsed.types)} 种类型，输出 {output}")
        if args.apply:
            from knext.schema.client import SchemaClient

            if settings.openspg_project_id <= 0:
                parser.exit(1, "请先配置专用 OpenSPG 项目 ID。\n")
            client = SchemaClient(settings.openspg_url, settings.openspg_project_id)
            session = client.create_session()
            existing = set(session.spg_types) & set(parsed.types)
            if existing:
                parser.exit(1, "目标领域类型已存在，未覆盖或迁移。请对比生成文件并人工设计迁移。\n")
            for value in parsed.types.values():
                session.create_type(value)
            session.commit()
            print("已提交新增领域类型；未删除或修改已有类型。")
    elif args.command == "doctor":
        settings = Settings()
        report = build_doctor_report(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
