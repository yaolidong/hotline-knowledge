"""探测本地 LLM、可选 Ollama pull、备份并写回应用 .env；不打印 API 密钥。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from hotline.local_llm import (
    apply_env_updates,
    is_local_preset_url,
    probe_local_llm,
    should_update_timeout,
)

ProbeFn = Callable[..., dict[str, Any]]
PullFn = Callable[[str], None]
SleepFn = Callable[[float], None]

_CONFIGURE_PROBE_TIMEOUT = 300.0

_DOCKER_MISSING_MSG = (
    "未找到 docker 命令，无法自动拉取模型。"
    "请先在 WSL 中按 deploy/README §5.4.2 启动 Ollama（compose 的 --profile 须与 deploy/llm.env 中 "
    "LLM_COMPOSE_PROFILE 一致，如 ollama 或 ollama-gpu），再手动执行 ollama pull 或使用 --skip-pull。"
)


def windows_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[-1]
    return f"/mnt/{drive}{tail}"


def resolve_docker_invocation() -> tuple[list[str], bool]:
    """返回 (docker 命令前缀, 是否用 WSL 风格路径)。"""
    if shutil.which("docker"):
        return ["docker"], False
    if sys.platform == "win32" and shutil.which("wsl"):
        return ["wsl", "-u", "root", "-e", "docker"], True
    return [], False


def resolve_ollama_pull_target(llm_values: dict[str, str | None] | None = None) -> tuple[str, str]:
    """返回 (compose profile, exec 目标服务名)。"""
    values = llm_values if llm_values is not None else dotenv_values("deploy/llm.env")
    profile = (values.get("LLM_COMPOSE_PROFILE") or "").strip()
    if not profile:
        engine = (values.get("LLM_ENGINE") or "").strip()
        if engine == "vllm":
            raise SystemExit("vLLM 引擎无需 Ollama pull。")
        profile = "ollama"
    if profile == "ollama-gpu":
        return profile, "ollama-gpu"
    if profile == "ollama":
        return profile, "ollama"
    raise SystemExit(f"deploy/llm.env 中 LLM_COMPOSE_PROFILE={profile!r} 无法用于 Ollama pull。")


def build_ollama_pull_argv(
    model: str,
    *,
    use_wsl_paths: bool,
    profile: str,
    service: str,
) -> list[str]:
    prefix, _ = resolve_docker_invocation()
    env_file = "deploy/llm.env"
    compose_file = "deploy/compose.llm.yml"
    if use_wsl_paths:
        root = Path.cwd()
        env_file = windows_path_to_wsl(root / env_file)
        compose_file = windows_path_to_wsl(root / compose_file)
    return [
        *prefix,
        "compose",
        "--project-name",
        "hotline-llm",
        "--env-file",
        env_file,
        "-f",
        compose_file,
        "--profile",
        profile,
        "exec",
        "-T",
        service,
        "ollama",
        "pull",
        model,
    ]


def default_pull(model: str) -> None:
    prefix, use_wsl_paths = resolve_docker_invocation()
    if not prefix:
        raise SystemExit(_DOCKER_MISSING_MSG)
    profile, service = resolve_ollama_pull_target()
    argv = build_ollama_pull_argv(
        model,
        use_wsl_paths=use_wsl_paths,
        profile=profile,
        service=service,
    )
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, OSError) as exc:
        raise SystemExit(f"{_DOCKER_MISSING_MSG}（{exc}）") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        raise SystemExit(f"Ollama pull 失败：{detail}")


def _require_paths() -> tuple[Path, Path]:
    llm_env = Path("deploy/llm.env")
    app_env = Path(".env")
    if not llm_env.is_file():
        raise SystemExit("缺少 deploy/llm.env。")
    if not app_env.is_file():
        raise SystemExit("缺少 .env。")
    return llm_env, app_env


def _run_probe(
    probe: ProbeFn,
    *,
    base_url: str,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    return probe(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=_CONFIGURE_PROBE_TIMEOUT,
        trust_env=False,
    )


def _probe_until_ok(
    *,
    engine: str,
    base_url: str,
    model: str,
    api_key: str,
    skip_pull: bool,
    probe: ProbeFn,
    pull: PullFn,
    sleep: SleepFn,
    retry_count: int,
    retry_interval: float,
) -> dict[str, Any]:
    report = _run_probe(probe, base_url=base_url, model=model, api_key=api_key)

    if engine == "ollama" and report.get("status") == "model_missing" and not skip_pull:
        pull(model)
        report = _run_probe(probe, base_url=base_url, model=model, api_key=api_key)
        return report

    if engine != "vllm":
        return report

    attempt = 0
    while report.get("status") in ("unreachable", "model_missing") and attempt < retry_count:
        sleep(retry_interval)
        attempt += 1
        report = _run_probe(probe, base_url=base_url, model=model, api_key=api_key)
    return report


def configure(
    *,
    force: bool = False,
    skip_pull: bool = False,
    probe: ProbeFn | None = None,
    pull: PullFn | None = None,
    sleep: SleepFn | None = None,
    retry_count: int = 3,
    retry_interval: float = 10.0,
) -> None:
    _require_paths()
    llm_values = dotenv_values("deploy/llm.env")
    app_values = dotenv_values(".env")

    engine = (llm_values.get("LLM_ENGINE") or "").strip()
    model = (llm_values.get("LLM_MODEL") or "").strip()
    port = (llm_values.get("LLM_HOST_PORT") or "").strip()
    api_key = llm_values.get("LLM_API_KEY") or ""

    if not engine or not model or not port:
        raise SystemExit("deploy/llm.env 缺少 LLM_ENGINE、LLM_MODEL 或 LLM_HOST_PORT。")

    base_url = f"http://127.0.0.1:{port}/v1"

    existing_url = app_values.get("HOTLINE_LLM_BASE_URL") or ""
    if existing_url and not is_local_preset_url(existing_url) and not force:
        raise SystemExit("已有其他 LLM 服务配置，未覆盖。")

    probe_fn = probe if probe is not None else probe_local_llm
    pull_fn = pull if pull is not None else default_pull
    sleep_fn = sleep if sleep is not None else time.sleep

    report = _probe_until_ok(
        engine=engine,
        base_url=base_url,
        model=model,
        api_key=api_key,
        skip_pull=skip_pull,
        probe=probe_fn,
        pull=pull_fn,
        sleep=sleep_fn,
        retry_count=retry_count,
        retry_interval=retry_interval,
    )

    if report.get("status") != "ok":
        detail = report.get("detail") or f"探测失败：{report.get('status', 'error')}"
        raise SystemExit(detail)

    app_env = Path(".env")
    backup_dir = Path("data/config-backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(app_env, backup_dir / ("env-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))

    content = app_env.read_text(encoding="utf-8")
    updates: dict[str, str] = {
        "HOTLINE_LLM_BACKEND": "openai",
        "HOTLINE_LLM_BASE_URL": base_url,
        "HOTLINE_LLM_MODEL": model,
        "HOTLINE_LLM_API_KEY": api_key,
        "HOTLINE_LLM_EXTRA_BODY": "{}",
    }
    if should_update_timeout(app_values.get("HOTLINE_LLM_TIMEOUT")):
        updates["HOTLINE_LLM_TIMEOUT"] = "300"

    app_env.write_text(apply_env_updates(content, updates), encoding="utf-8")
    print(json.dumps({"configured": True, "base_url": base_url, "model": model}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="探测本地 LLM 并写回应用 .env。")
    parser.add_argument("--force", action="store_true", help="允许从非本地 preset 覆盖现有 LLM 配置。")
    parser.add_argument("--skip-pull", action="store_true", help="Ollama 模型缺失时不执行 docker pull。")
    args = parser.parse_args(argv)
    configure(force=args.force, skip_pull=args.skip_pull)


if __name__ == "__main__":
    main(sys.argv[1:])
