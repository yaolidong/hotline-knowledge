"""生成 deploy/llm.env 默认配置；已存在则拒绝覆盖。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

OLLAMA_IMAGE = "ollama/ollama:0.6.8"
VLLM_IMAGE = "vllm/vllm-openai:v0.8.5"

_TIER_PRESETS: dict[tuple[str, str], tuple[str, str]] = {
    ("ollama", "cpu"): ("qwen2.5:7b-instruct", "11434"),
    ("ollama", "gpu-entry"): ("qwen2.5:14b-instruct", "11434"),
    ("vllm", "gpu-strong"): ("Qwen/Qwen2.5-14B-Instruct", "8000"),
}


def compose_profile_for(engine: str, tier: str) -> str:
    if engine == "vllm":
        return "vllm"
    if tier == "gpu-entry":
        return "ollama-gpu"
    return "ollama"


def build_llm_env(*, engine: str, tier: str) -> str:
    if engine == "vllm" and tier == "cpu":
        raise ValueError("vLLM 不支持 cpu 档位。")
    preset = _TIER_PRESETS.get((engine, tier))
    if preset is None:
        raise ValueError(f"不支持的组合：engine={engine} tier={tier}")
    model, host_port = preset
    profile = compose_profile_for(engine, tier)
    lines = [
        f"LLM_ENGINE={engine}",
        f"LLM_COMPOSE_PROFILE={profile}",
        f"OLLAMA_IMAGE={OLLAMA_IMAGE}",
        f"VLLM_IMAGE={VLLM_IMAGE}",
        f"LLM_MODEL={model}",
        f"LLM_HOST_PORT={host_port}",
        "LLM_API_KEY=",
        "VLLM_GPU_MEMORY_UTILIZATION=0.9",
        "VLLM_MAX_MODEL_LEN=16384",
        "OLLAMA_KEEP_ALIVE=24h",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成 deploy/llm.env 本地 LLM 部署配置。")
    parser.add_argument("--engine", required=True, choices=["ollama", "vllm"])
    parser.add_argument(
        "--tier",
        choices=["cpu", "gpu-entry", "gpu-strong"],
        default=None,
        help="ollama 默认 cpu；vllm 默认 gpu-strong。",
    )
    args = parser.parse_args(argv)
    tier = args.tier or ("cpu" if args.engine == "ollama" else "gpu-strong")

    target = Path("deploy/llm.env")
    if target.exists():
        raise SystemExit("已有 deploy/llm.env，未覆盖。")

    if args.engine == "vllm" and tier == "cpu":
        raise SystemExit("vLLM 引擎不支持 cpu 档位，请使用 gpu-strong。")

    try:
        content = build_llm_env(engine=args.engine, tier=tier)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as output:
        output.write(content)
    print(f"已写入 {target}（engine={args.engine} tier={tier}）。")


if __name__ == "__main__":
    main(sys.argv[1:])
