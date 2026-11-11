import importlib.util
from pathlib import Path

import pytest


def load():
    path = Path("scripts/prepare_local_llm.py")
    spec = importlib.util.spec_from_file_location("prepare_local_llm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return load()


def test_build_llm_env_ollama_cpu(mod):
    text = mod.build_llm_env(engine="ollama", tier="cpu")
    assert "LLM_ENGINE=ollama" in text
    assert "LLM_COMPOSE_PROFILE=ollama" in text
    assert "LLM_MODEL=qwen2.5:7b-instruct" in text
    assert "LLM_HOST_PORT=11434" in text
    assert "OLLAMA_IMAGE=ollama/ollama:0.6.8" in text


def test_build_llm_env_ollama_gpu_entry(mod):
    text = mod.build_llm_env(engine="ollama", tier="gpu-entry")
    assert "LLM_COMPOSE_PROFILE=ollama-gpu" in text
    assert "LLM_MODEL=qwen2.5:14b-instruct" in text


def test_build_llm_env_vllm(mod):
    text = mod.build_llm_env(engine="vllm", tier="gpu-strong")
    assert "LLM_ENGINE=vllm" in text
    assert "LLM_COMPOSE_PROFILE=vllm" in text
    assert "LLM_MODEL=Qwen/Qwen2.5-14B-Instruct" in text
    assert "LLM_HOST_PORT=8000" in text
    assert "VLLM_IMAGE=vllm/vllm-openai:v0.8.5" in text


def test_main_refuses_overwrite(tmp_path, monkeypatch, mod):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "llm.env").write_text("LLM_ENGINE=ollama\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.main(["--engine", "ollama"])
