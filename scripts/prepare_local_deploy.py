"""解析上游官方镜像的不可变摘要，生成本机专用密码；不输出密码或覆盖已有部署配置。"""

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

REGISTRY = "spg-registry.us-west-1.cr.aliyuncs.com"
IMAGES = {"server": "0.8-amd64", "mysql": "0.8-amd64", "neo4j": "0.5.1", "minio": "0.6-amd64"}


def resolve(kind):
    name = f"spg/openspg-{kind}"
    with httpx.Client(timeout=60, trust_env=False) as client:
        base = f"https://{REGISTRY}/v2/{name}/manifests/"
        url = base + IMAGES[kind]
        headers = {
            "Accept": ", ".join(
                [
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.oci.image.index.v1+json",
                ]
            )
        }
        response = client.get(url, headers=headers)
        if response.status_code == 401:
            challenge = dict(re.findall(r'(\w+)="([^"]+)"', response.headers["www-authenticate"]))
            token = client.get(challenge["realm"], params={k: challenge[k] for k in ("service", "scope")})
            token.raise_for_status()
            headers["Authorization"] = "Bearer " + token.json()["token"]
            response = client.get(url, headers=headers)
        response.raise_for_status()
        if "manifests" in response.json():
            descriptor = next(
                m
                for m in response.json()["manifests"]
                if m.get("platform", {}).get("architecture") == "amd64" and m["platform"].get("os") == "linux"
            )
            response = client.get(base + descriptor["digest"], headers=headers)
            response.raise_for_status()
        digest = response.headers["docker-content-digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("镜像摘要格式不合法")
        return kind, f"{REGISTRY}/{name}@{digest}"


def main():
    target = Path("deploy/images.env")
    if target.exists():
        raise SystemExit("已有 deploy/images.env，未覆盖。")
    images = dict(ThreadPoolExecutor(4).map(resolve, IMAGES))
    mysql_password, graph_password = secrets.token_hex(20), secrets.token_hex(20)
    lines = [f"OPENSPG_{kind.upper()}_IMAGE={image}" for kind, image in images.items()]
    lines += [
        f"MYSQL_PASSWORD={mysql_password}",
        f"GRAPH_PASSWORD={graph_password}",
        f"EMBEDDING_API_KEY={secrets.token_hex(24)}",
    ]
    with target.open("x", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    Path("deploy/images.lock.json").write_text(json.dumps(images, indent=2) + "\n", encoding="utf-8")
    env = Path(".env")
    content = env.read_text(encoding="utf-8")
    for key, value in {"HOTLINE_NEO4J_PASSWORD": graph_password, "HOTLINE_NAMESPACE": "HotlineTest"}.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
    env.write_text(content, encoding="utf-8")
    print(
        json.dumps(
            {"images": images, "credentials": "已写入被 Git 忽略的本地配置"}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
